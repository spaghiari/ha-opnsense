"""Coordinator de polling pour OPNsense custom."""
from __future__ import annotations

import ipaddress
import logging
from datetime import timedelta
from time import monotonic
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .api import OPNsenseApiClient, OPNsenseApiError, OPNsenseAuthError
from .const import (
    DEFAULT_MODEL,
    DEFAULT_WAN_IDENTIFIER,
    DOMAIN,
    FAST_ENDPOINTS,
    MANUFACTURER,
    SLOW_ENDPOINTS,
    WAN_AUTO,
)

_LOGGER = logging.getLogger(__name__)


def _is_private_ipv4(addr: str) -> bool:
    """True si l'IPv4 est privée / loopback / link-local (donc pas un WAN public)."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return True
    return ip.is_private or ip.is_loopback or ip.is_link_local


def resolve_wan_device(rows: Any, configured: str | None) -> str | None:
    """Détermine le device (ex: 'igc0') de l'interface WAN.

    Si l'utilisateur a explicitement choisi une interface, on l'honore.
    Sinon on auto-détecte selon, dans l'ordre :
      1. une interface décrite "WAN" (insensible à la casse) - rétro-compat ;
      2. une interface portant une gateway (route par défaut) ;
      3. une interface avec une IPv4 publique.
    Renvoie None si rien ne correspond.
    """
    if not isinstance(rows, list):
        return None

    # Choix explicite de l'utilisateur (s'il existe encore dans la liste)
    if configured and configured != WAN_AUTO:
        for row in rows:
            if isinstance(row, dict) and row.get("device") == configured:
                return configured

    # 1. Description "WAN"
    for row in rows:
        if isinstance(row, dict) and (
            (row.get("description") or "").strip().upper() == "WAN"
        ):
            return row.get("device")

    # 2. Interface avec une gateway active (route par défaut)
    for row in rows:
        if isinstance(row, dict):
            gateways = row.get("gateways")
            if isinstance(gateways, list) and gateways:
                return row.get("device")

    # 3. Interface avec une IPv4 publique
    for row in rows:
        if isinstance(row, dict):
            addr = (row.get("addr4") or "").split("/")[0]
            if addr and not _is_private_ipv4(addr):
                return row.get("device")

    return None


def find_wan_row(data: dict) -> dict | None:
    """Renvoie la row de l'interface WAN résolue (via data['_wan_device'])."""
    target = data.get("_wan_device")
    if not target:
        return None
    rows = (data.get("interfaces") or {}).get("rows")
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and row.get("device") == target:
                return row
    return None


def build_device_info(entry: ConfigEntry, data: dict | None) -> DeviceInfo:
    """Construit le DeviceInfo commun à toutes les entités d'un firewall.

    Le nom reste stable ("OPNsense") pour garantir des entity_ids
    prévisibles (`sensor.opnsense_*`) et donc un dashboard portable ; les
    setups multi-firewalls renomment le device dans l'UI. La version
    logicielle est extraite des `versions` quand elle est disponible.
    """
    host = entry.data.get("host", "OPNsense")
    sw_version: str | None = None
    if data:
        info = data.get("system_information") or {}
        for version in info.get("versions") or []:
            if isinstance(version, str) and version.startswith("OPNsense"):
                sw_version = version.replace("OPNsense ", "")
                break
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="OPNsense",
        manufacturer=MANUFACTURER,
        model=DEFAULT_MODEL,
        sw_version=sw_version,
        configuration_url=f"https://{host}",
    )


def _wan_counters(data: dict) -> tuple[int, int] | None:
    """Compteurs d'octets (reçus, émis) de l'interface WAN résolue."""
    device = data.get("_wan_device")
    interfaces = (data.get("traffic_totals") or {}).get("interfaces")
    if not device or not isinstance(interfaces, dict):
        return None
    for iface in interfaces.values():
        if isinstance(iface, dict) and iface.get("device") == device:
            try:
                return (int(iface["bytes received"]),
                        int(iface["bytes transmitted"]))
            except (KeyError, TypeError, ValueError):
                return None
    return None


class OPNsenseFastCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polling rapide : interfaces, passerelles et compteurs WAN.

    Résout l'interface WAN (device + identifiant de config) et calcule un
    débit moyen exact à partir des compteurs d'octets (différence / temps),
    utilisé quand le flux temps réel n'est pas disponible.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        client: OPNsenseApiClient,
        interval: int,
        entry: ConfigEntry,
        wan_interface: str = WAN_AUTO,
    ) -> None:
        """Initialise le coordinator rapide."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}_fast",
            update_interval=timedelta(seconds=interval),
        )
        self.client = client
        self.entry = entry
        self.wan_interface = wan_interface
        self.wan_identifier = DEFAULT_WAN_IDENTIFIER
        self._prev_counters: tuple[float, int, int] | None = None

    async def _async_update_data(self) -> dict[str, Any]:
        """Appelé toutes les `fast_interval` secondes."""
        try:
            data = await self.client.async_get_all(keys=FAST_ENDPOINTS)
        except OPNsenseAuthError as err:
            raise ConfigEntryAuthFailed(
                "Clé API OPNsense invalide - reconfiguration nécessaire"
            ) from err
        except OPNsenseApiError as err:
            raise UpdateFailed(f"Erreur API OPNsense: {err}") from err
        if data.get("interfaces") is None:
            raise UpdateFailed(
                "Impossible de récupérer les interfaces - vérifier les privilèges API"
            )

        rows = (data.get("interfaces") or {}).get("rows")
        data["_wan_device"] = resolve_wan_device(rows, self.wan_interface)
        identifier = (find_wan_row(data) or {}).get("identifier")
        if identifier:
            self.wan_identifier = identifier
        data["_wan_identifier"] = self.wan_identifier

        # Débit moyen depuis le cycle précédent, à partir des compteurs.
        data["_wan_rate"] = None
        counters = _wan_counters(data)
        now = monotonic()
        if counters is not None:
            if self._prev_counters is not None:
                t0, rx0, tx0 = self._prev_counters
                elapsed = now - t0
                drx, dtx = counters[0] - rx0, counters[1] - tx0
                # Compteurs remis à zéro (reboot) : on saute ce cycle.
                if elapsed > 0 and drx >= 0 and dtx >= 0:
                    data["_wan_rate"] = {
                        "in_bps": int(drx * 8 / elapsed),
                        "out_bps": int(dtx * 8 / elapsed),
                    }
            self._prev_counters = (now, counters[0], counters[1])
        return data


class OPNsenseDataCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polling lent : firmware, système, disque, services, top destinations.

    C'est l'objet "principal" de l'entry (stocké dans hass.data) : il porte
    aussi le coordinator rapide (`fast`) et le flux temps réel (`live`), et
    expose `merged`, la vue fusionnée lue par toutes les entités.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        client: OPNsenseApiClient,
        scan_interval: int,
        entry: ConfigEntry,
        fast: OPNsenseFastCoordinator,
    ) -> None:
        """Initialise le coordinator lent."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(seconds=scan_interval),
        )
        self.client = client
        self.entry = entry
        self.fast = fast
        self.live: Any = None  # OPNsenseLiveCoordinator, posé par __init__.py

    @property
    def merged(self) -> dict[str, Any]:
        """Vue fusionnée lent + rapide + temps réel (s'il est frais)."""
        merged: dict[str, Any] = dict(self.data or {})
        merged.update(self.fast.data or {})
        if self.live is not None:
            merged["_live"] = self.live.fresh_data()
        return merged

    async def _async_update_data(self) -> dict[str, Any]:
        """Appelé toutes les `scan_interval` secondes."""
        identifier = self.fast.wan_identifier
        try:
            data = await self.client.async_get_all(identifier, keys=SLOW_ENDPOINTS)
        except OPNsenseAuthError as err:
            raise ConfigEntryAuthFailed(
                "Clé API OPNsense invalide - reconfiguration nécessaire"
            ) from err
        except OPNsenseApiError as err:
            raise UpdateFailed(f"Erreur API OPNsense: {err}") from err

        if data.get("system_information") is None:
            raise UpdateFailed(
                "Impossible de récupérer system_information - "
                "vérifier les privilèges API"
            )

        # La réponse top/{iface} est indexée par l'identifiant d'interface :
        # on la ramène sous la clé "wan" attendue par les capteurs.
        traffic = data.get("traffic_wan")
        if (
            isinstance(traffic, dict)
            and identifier != DEFAULT_WAN_IDENTIFIER
            and identifier in traffic
        ):
            data["traffic_wan"] = {DEFAULT_WAN_IDENTIFIER: traffic[identifier]}
        return data
