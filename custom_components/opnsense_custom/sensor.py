"""Sensors OPNsense custom."""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfDataRate,
    UnitOfInformation,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import OPNsenseDataCoordinator, build_device_info, find_wan_row

_LOGGER = logging.getLogger(__name__)


# ============================================================
#  Fonctions d'extraction (value_fn) pour chaque type de sensor
# ============================================================
#
# Chaque sensor reçoit le dict complet du coordinator et doit
# extraire sa propre valeur. Si la donnée est absente, renvoyer None.


def _get(data: dict, *keys: str) -> Any:
    """Accède en profondeur à un dict, renvoie None si une clé manque."""
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
        if current is None:
            return None
    return current


def _hostname(data: dict) -> str | None:
    return _get(data, "system_information", "name")


def _opnsense_version(data: dict) -> str | None:
    versions = _get(data, "system_information", "versions")
    if isinstance(versions, list) and versions:
        # Premier élément = OPNsense, ex: "OPNsense 26.1.8_5-amd64"
        first = versions[0]
        if isinstance(first, str) and first.startswith("OPNsense"):
            # Extrait "26.1.8_5-amd64"
            parts = first.split(" ", 1)
            return parts[1] if len(parts) > 1 else first
    return None


def _freebsd_version(data: dict) -> str | None:
    versions = _get(data, "system_information", "versions")
    if isinstance(versions, list):
        for v in versions:
            if isinstance(v, str) and v.startswith("FreeBSD"):
                return v.replace("FreeBSD ", "")
    return None


def _openssl_version(data: dict) -> str | None:
    versions = _get(data, "system_information", "versions")
    if isinstance(versions, list):
        for v in versions:
            if isinstance(v, str) and v.startswith("OpenSSL"):
                return v.replace("OpenSSL ", "")
    return None


def _cpu_model(data: dict) -> str | None:
    cpu_data = data.get("cpu_type")
    if isinstance(cpu_data, list) and cpu_data:
        return cpu_data[0]
    if isinstance(cpu_data, dict):
        return cpu_data.get("cpu") or cpu_data.get("name")
    return None


def _ram_total(data: dict) -> int | None:
    """RAM totale en octets."""
    val = _get(data, "system_resources", "memory", "total")
    return int(val) if val is not None else None


def _ram_used(data: dict) -> int | None:
    """RAM utilisée en octets."""
    val = _get(data, "system_resources", "memory", "used")
    return int(val) if val is not None else None


def _ram_used_percent(data: dict) -> float | None:
    """% RAM utilisée."""
    total = _ram_total(data)
    used = _ram_used(data)
    if total and used and total > 0:
        return round((used / total) * 100, 1)
    return None


def _root_disk(data: dict) -> dict | None:
    """Renvoie le dict du device monté sur '/'."""
    devices = _get(data, "system_disk", "devices")
    if isinstance(devices, list):
        for dev in devices:
            if isinstance(dev, dict) and dev.get("mountpoint") == "/":
                return dev
    return None


def _root_disk_used_percent(data: dict) -> float | None:
    dev = _root_disk(data)
    if dev:
        used_pct = dev.get("used_pct")
        return float(used_pct) if used_pct is not None else None
    return None


def _root_disk_blocks(data: dict) -> str | None:
    dev = _root_disk(data)
    return dev.get("blocks") if dev else None


def _root_disk_used(data: dict) -> str | None:
    dev = _root_disk(data)
    return dev.get("used") if dev else None


def _root_disk_available(data: dict) -> str | None:
    dev = _root_disk(data)
    return dev.get("available") if dev else None


def _uptime(data: dict) -> str | None:
    return _get(data, "system_time", "uptime")


def _boottime(data: dict) -> datetime | None:
    """Convertit la string boottime en datetime UTC pour HA."""
    raw = _get(data, "system_time", "boottime")
    if not isinstance(raw, str):
        return None
    # Format OPNsense : "Mon May 25 10:45:38 CEST 2026"
    for fmt in (
        "%a %b %d %H:%M:%S %Z %Y",
        "%a %b %d %H:%M:%S %z %Y",
    ):
        try:
            dt = datetime.strptime(raw, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt
        except ValueError:
            continue
    _LOGGER.debug("Format boottime non reconnu : %s", raw)
    return None


def _loadavg_1(data: dict) -> float | None:
    raw = _get(data, "system_time", "loadavg")
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",")]
        if parts:
            try:
                return float(parts[0])
            except ValueError:
                pass
    return None


def _loadavg_5(data: dict) -> float | None:
    raw = _get(data, "system_time", "loadavg")
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",")]
        if len(parts) > 1:
            try:
                return float(parts[1])
            except ValueError:
                pass
    return None


def _loadavg_15(data: dict) -> float | None:
    raw = _get(data, "system_time", "loadavg")
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",")]
        if len(parts) > 2:
            try:
                return float(parts[2])
            except ValueError:
                pass
    return None


def _wan_interface(data: dict) -> dict | None:
    """Renvoie la row de l'interface WAN (résolue par le coordinator).

    Le device WAN est déterminé une fois par cycle dans le coordinator
    (choix utilisateur ou auto-détection) et injecté dans data['_wan_device'].
    """
    return find_wan_row(data)


def _public_ipv4(data: dict) -> str | None:
    """IPv4 publique = IP de l'interface WAN."""
    wan = _wan_interface(data)
    if wan:
        # addr4 contient l'IP réelle (ex: "1.2.3.4/24"), ipaddr le mode ("dhcp")
        addr = wan.get("addr4") or wan.get("ipaddr")
        if isinstance(addr, str) and addr not in ("dhcp", "none", ""):
            return addr.split("/")[0]  # On retire le masque
    return None


def _public_ipv6(data: dict) -> str | None:
    """IPv6 globale du WAN (pas le link-local fe80::)."""
    wan = _wan_interface(data)
    if wan:
        addr = wan.get("addr6")
        if isinstance(addr, str) and addr and not addr.lower().startswith("fe80"):
            return addr.split("/")[0]
    return None


def _wan_status(data: dict) -> str | None:
    wan = _wan_interface(data)
    return wan.get("status") if wan else None


def _firmware_installed(data: dict) -> str | None:
    """Version OPNsense actuellement installée."""
    return _get(data, "firmware_status", "product", "product_version") or _get(
        data, "firmware_status", "product_version"
    )


def _firmware_latest(data: dict) -> str | None:
    """Dernière version disponible (peut être None si pas encore checké)."""
    return _get(data, "firmware_status", "product", "product_latest") or _get(
        data, "firmware_status", "product_latest"
    )


# ----- Trafic WAN -----
#
# Deux endpoints sont utilisés :
#  - traffic_wan = /api/diagnostics/traffic/top/wan
#    → débit temps réel pré-calculé par OPNsense (rate_bits_in/out en bps)
#  - traffic_totals = /api/diagnostics/traffic/interface
#    → compteurs cumulés depuis le boot (bytes received/transmitted en octets)
#
# On retrouve l'interface WAN par son device (ex: igc0) déjà identifié
# via interfacesInfo pour ne pas dépendre de la nomenclature OPNsense.


def _wan_device_name(data: dict) -> str | None:
    """Nom du device de l'interface WAN (ex: 'igc0')."""
    wan = _wan_interface(data)
    return wan.get("device") if wan else None


def _traffic_in_bps(data: dict) -> int | None:
    """Débit entrant WAN total en bits par seconde (temps réel).

    L'endpoint /traffic/top/wan renvoie les débits par destination dans
    une liste 'records'. Le débit global = somme des rate_bits_in.
    """
    records = _get(data, "traffic_wan", "wan", "records")
    if not isinstance(records, list):
        return None
    total = 0
    found = False
    for rec in records:
        if isinstance(rec, dict):
            val = rec.get("rate_bits_in")
            if val is not None:
                try:
                    total += int(val)
                    found = True
                except (TypeError, ValueError):
                    continue
    return total if found else None


def _traffic_out_bps(data: dict) -> int | None:
    """Débit sortant WAN total en bits par seconde (temps réel).

    Même logique : somme des rate_bits_out de toutes les destinations.
    """
    records = _get(data, "traffic_wan", "wan", "records")
    if not isinstance(records, list):
        return None
    total = 0
    found = False
    for rec in records:
        if isinstance(rec, dict):
            val = rec.get("rate_bits_out")
            if val is not None:
                try:
                    total += int(val)
                    found = True
                except (TypeError, ValueError):
                    continue
    return total if found else None


def _traffic_total_received(data: dict) -> int | None:
    """Total octets reçus sur le WAN depuis le dernier reset des compteurs."""
    device = _wan_device_name(data)
    if not device:
        return None
    interfaces = _get(data, "traffic_totals", "interfaces")
    if not isinstance(interfaces, dict):
        return None
    for iface_data in interfaces.values():
        if isinstance(iface_data, dict) and iface_data.get("device") == device:
            # Attention : la clé contient un ESPACE, format OPNsense
            val = iface_data.get("bytes received")
            try:
                return int(val) if val is not None else None
            except (TypeError, ValueError):
                return None
    return None


def _traffic_total_transmitted(data: dict) -> int | None:
    """Total octets transmis sur le WAN depuis le dernier reset des compteurs."""
    device = _wan_device_name(data)
    if not device:
        return None
    interfaces = _get(data, "traffic_totals", "interfaces")
    if not isinstance(interfaces, dict):
        return None
    for iface_data in interfaces.values():
        if isinstance(iface_data, dict) and iface_data.get("device") == device:
            val = iface_data.get("bytes transmitted")
            try:
                return int(val) if val is not None else None
            except (TypeError, ValueError):
                return None
    return None


# ----- Top destinations WAN -----
#
# On expose 2 sensors :
#   - state = nom de la #1 destination (reverse DNS ou IP fallback)
#   - attribut "top_5" = liste des 5 plus gros consommateurs avec nom + débit
#
# Le tri se fait sur rate_bits_in (ou _out) pour respectivement le download
# et l'upload. On retire les destinations "local" (LAN→WAN intra-réseau).

TOP_N = 5


def _dest_display_name(rec: dict) -> str:
    """Renvoie le reverse DNS si présent, sinon l'IP brute.

    OPNsense met le DNS résolu dans 'rname' (avec un point final
    de notation FQDN qu'on retire pour la lisibilité).
    """
    rname = rec.get("rname")
    if isinstance(rname, str) and rname and rname != rec.get("address"):
        return rname.rstrip(".")
    return rec.get("address") or "?"


def _is_local_record(rec: dict) -> bool:
    """Filtre les destinations marquées 'local' (trafic interne LAN<->WAN)."""
    tags = rec.get("tags") or []
    return isinstance(tags, list) and "local" in tags


def _top_destinations(data: dict, direction: str) -> list[dict] | None:
    """Top N destinations triées par débit dans la direction donnée.

    direction = 'in'  -> rate_bits_in  (téléchargement)
    direction = 'out' -> rate_bits_out (téléversement)

    Renvoie une liste de dicts {name, address, rate_bps, rate_mbps},
    triée du plus gros au plus petit, limitée à TOP_N entrées.
    """
    records = _get(data, "traffic_wan", "wan", "records")
    if not isinstance(records, list) or not records:
        return None

    rate_key = "rate_bits_in" if direction == "in" else "rate_bits_out"
    candidates: list[dict] = []
    for rec in records:
        if not isinstance(rec, dict) or _is_local_record(rec):
            continue
        try:
            rate = int(rec.get(rate_key, 0))
        except (TypeError, ValueError):
            continue
        if rate <= 0:
            continue
        candidates.append(
            {
                "name": _dest_display_name(rec),
                "address": rec.get("address"),
                "rate_bps": rate,
                "rate_mbps": round(rate / 1_000_000, 2),
            }
        )

    candidates.sort(key=lambda x: x["rate_bps"], reverse=True)
    return candidates[:TOP_N] if candidates else None


def _top_dest_in_name(data: dict) -> str | None:
    """Nom (DNS ou IP) de la première destination en download."""
    top = _top_destinations(data, "in")
    return top[0]["name"] if top else None


def _top_dest_out_name(data: dict) -> str | None:
    """Nom (DNS ou IP) de la première destination en upload."""
    top = _top_destinations(data, "out")
    return top[0]["name"] if top else None


# ============================================================
#  Description de chaque sensor
# ============================================================


SENSOR_DESCRIPTIONS: tuple[tuple[SensorEntityDescription, Callable], ...] = (
    (
        SensorEntityDescription(
            key="hostname",
            translation_key="hostname",
            icon="mdi:server-network",
        ),
        _hostname,
    ),
    (
        SensorEntityDescription(
            key="opnsense_version",
            translation_key="opnsense_version",
            icon="mdi:tag",
        ),
        _opnsense_version,
    ),
    (
        SensorEntityDescription(
            key="freebsd_version",
            translation_key="freebsd_version",
            icon="mdi:freebsd",
            entity_registry_enabled_default=False,
        ),
        _freebsd_version,
    ),
    (
        SensorEntityDescription(
            key="openssl_version",
            translation_key="openssl_version",
            icon="mdi:lock-outline",
            entity_registry_enabled_default=False,
        ),
        _openssl_version,
    ),
    (
        SensorEntityDescription(
            key="cpu_model",
            translation_key="cpu_model",
            icon="mdi:cpu-64-bit",
            entity_registry_enabled_default=False,
        ),
        _cpu_model,
    ),
    (
        SensorEntityDescription(
            key="ram_total",
            translation_key="ram_total",
            icon="mdi:memory",
            device_class=SensorDeviceClass.DATA_SIZE,
            native_unit_of_measurement=UnitOfInformation.BYTES,
            suggested_unit_of_measurement=UnitOfInformation.GIGABYTES,
            suggested_display_precision=2,
            entity_registry_enabled_default=False,
        ),
        _ram_total,
    ),
    (
        SensorEntityDescription(
            key="ram_used",
            translation_key="ram_used",
            icon="mdi:memory",
            device_class=SensorDeviceClass.DATA_SIZE,
            native_unit_of_measurement=UnitOfInformation.BYTES,
            suggested_unit_of_measurement=UnitOfInformation.GIGABYTES,
            suggested_display_precision=2,
            state_class=SensorStateClass.MEASUREMENT,
        ),
        _ram_used,
    ),
    (
        SensorEntityDescription(
            key="ram_used_percent",
            translation_key="ram_used_percent",
            icon="mdi:memory",
            native_unit_of_measurement=PERCENTAGE,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=1,
        ),
        _ram_used_percent,
    ),
    (
        SensorEntityDescription(
            key="disk_root_percent",
            translation_key="disk_root_percent",
            icon="mdi:harddisk",
            native_unit_of_measurement=PERCENTAGE,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=0,
        ),
        _root_disk_used_percent,
    ),
    (
        SensorEntityDescription(
            key="disk_root_total",
            translation_key="disk_root_total",
            icon="mdi:harddisk",
            entity_registry_enabled_default=False,
        ),
        _root_disk_blocks,
    ),
    (
        SensorEntityDescription(
            key="disk_root_used",
            translation_key="disk_root_used",
            icon="mdi:harddisk",
            entity_registry_enabled_default=False,
        ),
        _root_disk_used,
    ),
    (
        SensorEntityDescription(
            key="disk_root_available",
            translation_key="disk_root_available",
            icon="mdi:harddisk",
            entity_registry_enabled_default=False,
        ),
        _root_disk_available,
    ),
    (
        SensorEntityDescription(
            key="uptime",
            translation_key="uptime",
            icon="mdi:clock-outline",
        ),
        _uptime,
    ),
    (
        SensorEntityDescription(
            key="boottime",
            translation_key="boottime",
            icon="mdi:restart",
            device_class=SensorDeviceClass.TIMESTAMP,
        ),
        _boottime,
    ),
    (
        SensorEntityDescription(
            key="loadavg_1",
            translation_key="loadavg_1",
            icon="mdi:gauge",
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=2,
        ),
        _loadavg_1,
    ),
    (
        SensorEntityDescription(
            key="loadavg_5",
            translation_key="loadavg_5",
            icon="mdi:gauge",
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=2,
            entity_registry_enabled_default=False,
        ),
        _loadavg_5,
    ),
    (
        SensorEntityDescription(
            key="loadavg_15",
            translation_key="loadavg_15",
            icon="mdi:gauge",
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=2,
            entity_registry_enabled_default=False,
        ),
        _loadavg_15,
    ),
    (
        SensorEntityDescription(
            key="public_ipv4",
            translation_key="public_ipv4",
            icon="mdi:ip-network",
        ),
        _public_ipv4,
    ),
    (
        SensorEntityDescription(
            key="public_ipv6",
            translation_key="public_ipv6",
            icon="mdi:ip-network-outline",
            entity_registry_enabled_default=False,
        ),
        _public_ipv6,
    ),
    (
        SensorEntityDescription(
            key="wan_status",
            translation_key="wan_status",
            icon="mdi:lan-connect",
            entity_registry_enabled_default=False,
        ),
        _wan_status,
    ),
    (
        SensorEntityDescription(
            key="firmware_installed",
            translation_key="firmware_installed",
            icon="mdi:package-variant-closed",
            entity_registry_enabled_default=False,
        ),
        _firmware_installed,
    ),
    (
        SensorEntityDescription(
            key="firmware_latest",
            translation_key="firmware_latest",
            icon="mdi:package-up",
            entity_registry_enabled_default=False,
        ),
        _firmware_latest,
    ),
    # ----- Trafic WAN -----
    (
        SensorEntityDescription(
            key="wan_throughput_in",
            translation_key="wan_throughput_in",
            icon="mdi:download-network",
            device_class=SensorDeviceClass.DATA_RATE,
            native_unit_of_measurement=UnitOfDataRate.BITS_PER_SECOND,
            suggested_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND,
            suggested_display_precision=2,
            state_class=SensorStateClass.MEASUREMENT,
        ),
        _traffic_in_bps,
    ),
    (
        SensorEntityDescription(
            key="wan_throughput_out",
            translation_key="wan_throughput_out",
            icon="mdi:upload-network",
            device_class=SensorDeviceClass.DATA_RATE,
            native_unit_of_measurement=UnitOfDataRate.BITS_PER_SECOND,
            suggested_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND,
            suggested_display_precision=2,
            state_class=SensorStateClass.MEASUREMENT,
        ),
        _traffic_out_bps,
    ),
    (
        SensorEntityDescription(
            key="wan_total_received",
            translation_key="wan_total_received",
            icon="mdi:download",
            device_class=SensorDeviceClass.DATA_SIZE,
            native_unit_of_measurement=UnitOfInformation.BYTES,
            suggested_unit_of_measurement=UnitOfInformation.GIGABYTES,
            suggested_display_precision=2,
            state_class=SensorStateClass.TOTAL_INCREASING,
        ),
        _traffic_total_received,
    ),
    (
        SensorEntityDescription(
            key="wan_total_transmitted",
            translation_key="wan_total_transmitted",
            icon="mdi:upload",
            device_class=SensorDeviceClass.DATA_SIZE,
            native_unit_of_measurement=UnitOfInformation.BYTES,
            suggested_unit_of_measurement=UnitOfInformation.GIGABYTES,
            suggested_display_precision=2,
            state_class=SensorStateClass.TOTAL_INCREASING,
        ),
        _traffic_total_transmitted,
    ),
    (
        SensorEntityDescription(
            key="wan_top_dest_in",
            translation_key="wan_top_dest_in",
            icon="mdi:trophy-outline",
        ),
        _top_dest_in_name,
    ),
    (
        SensorEntityDescription(
            key="wan_top_dest_out",
            translation_key="wan_top_dest_out",
            icon="mdi:trophy-outline",
        ),
        _top_dest_out_name,
    ),
)


# Mapping sensor_key -> fonction qui extrait les attributs supplémentaires.
# Permet d'exposer top_5 sur les sensors top_dest sans toucher aux autres.
ATTRIBUTE_EXTRACTORS = {
    "wan_top_dest_in": lambda data: {"top_5": _top_destinations(data, "in") or []},
    "wan_top_dest_out": lambda data: {"top_5": _top_destinations(data, "out") or []},
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Crée tous les sensors à partir des descriptions ci-dessus."""
    coordinator: OPNsenseDataCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = [
        OPNsenseSensor(coordinator, entry, description, value_fn)
        for description, value_fn in SENSOR_DESCRIPTIONS
    ]
    async_add_entities(entities)


class OPNsenseSensor(CoordinatorEntity[OPNsenseDataCoordinator], SensorEntity):
    """Sensor générique alimenté par le coordinator."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OPNsenseDataCoordinator,
        entry: ConfigEntry,
        description: SensorEntityDescription,
        value_fn: Callable[[dict], Any],
    ) -> None:
        """Initialise le sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._value_fn = value_fn
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        # Toutes les entités rattachées au même appareil firewall
        self._attr_device_info = build_device_info(entry, coordinator.data)

    @property
    def native_value(self) -> Any:
        """Lit la valeur depuis le snapshot du coordinator."""
        if self.coordinator.data is None:
            return None
        return self._value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose des attributs supplémentaires pour certains sensors.

        Utilisé notamment pour wan_top_dest_in/out qui exposent un top_5
        complet sous forme de liste consultable depuis Lovelace.
        """
        extractor = ATTRIBUTE_EXTRACTORS.get(self.entity_description.key)
        if extractor is None or self.coordinator.data is None:
            return None
        try:
            return extractor(self.coordinator.data)
        except Exception:  # noqa: BLE001
            return None
