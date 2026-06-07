"""Coordinator de polling pour OPNsense custom."""
from __future__ import annotations

import ipaddress
import logging
from datetime import timedelta
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
from .const import DEFAULT_MODEL, DOMAIN, MANUFACTURER, WAN_AUTO

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


class OPNsenseDataCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator qui appelle async_get_all() périodiquement.

    Toutes les entités (sensors, binary_sensors, update) lisent
    self.data pour leurs valeurs. Évite que chaque entité fasse
    son propre appel HTTP.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        client: OPNsenseApiClient,
        scan_interval: int,
        entry: ConfigEntry,
        wan_interface: str = WAN_AUTO,
    ) -> None:
        """Initialise le coordinator avec un intervalle de polling."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(seconds=scan_interval),
        )
        self.client = client
        self.entry = entry
        self.wan_interface = wan_interface

    async def _async_update_data(self) -> dict[str, Any]:
        """Appelé automatiquement toutes les `scan_interval` secondes."""
        try:
            data = await self.client.async_get_all()
        except OPNsenseAuthError as err:
            # Clé API invalide/révoquée : déclenche le flux de ré-authentification
            raise ConfigEntryAuthFailed(
                "Clé API OPNsense invalide - reconfiguration nécessaire"
            ) from err
        except OPNsenseApiError as err:
            raise UpdateFailed(f"Erreur API OPNsense: {err}") from err

        # On vérifie qu'on a au moins les infos système, sinon ça ne sert à rien
        if data.get("system_information") is None:
            raise UpdateFailed(
                "Impossible de récupérer system_information - "
                "vérifier les privilèges API"
            )

        # Résout une fois par cycle le device WAN et l'injecte pour les entités
        rows = (data.get("interfaces") or {}).get("rows")
        data["_wan_device"] = resolve_wan_device(rows, self.wan_interface)

        return data
