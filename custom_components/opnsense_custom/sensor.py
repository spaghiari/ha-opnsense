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
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import (
    OPNsenseDataCoordinator,
    build_device_info,
    find_wan_row,
    wan_counters,
)
from .wans import (
    default_link,
    find_group,
    find_link,
    leading_float,
    links_with_entities,
)

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
    """Occupation du disque système en %.

    En ZFS, le dataset monté sur "/" ne compte que ses propres données (les
    logs, /usr, /var... sont dans d'autres datasets du même pool) : son
    pourcentage reste proche de 0 % sur un gros disque. On calcule donc le
    taux du pool : somme des octets utilisés par ses datasets / (cette somme
    + espace libre du pool). En UFS, même formule que df sur "/".
    """
    dev = _root_disk(data)
    if not dev:
        return None
    if dev.get("type") == "zfs" and dev.get("available_bytes") is not None:
        pool = str(dev.get("device") or "").split("/")[0]
        devices = _get(data, "system_disk", "devices") or []
        used = 0
        for ds in devices:
            if not isinstance(ds, dict) or ds.get("type") != "zfs":
                continue
            name = str(ds.get("device") or "")
            if name == pool or name.startswith(pool + "/"):
                try:
                    used += int(ds.get("used_bytes") or 0)
                except (TypeError, ValueError):
                    continue
        try:
            total = used + int(dev["available_bytes"])
        except (TypeError, ValueError):
            total = 0
        if total > 0:
            return round(used / total * 100, 1)
    # UFS & co : même calcul que df (utilisé / (utilisé + libre)), mais avec
    # une décimale - df arrondit à l'entier, 0 % sur un gros disque.
    try:
        used, free = int(dev["used_bytes"]), int(dev["available_bytes"])
        if used + free > 0:
            return round(used / (used + free) * 100, 1)
    except (KeyError, TypeError, ValueError):
        pass
    used_pct = dev.get("used_pct")
    try:
        return float(used_pct) if used_pct is not None else None
    except (TypeError, ValueError):
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
    """Convertit la string boottime en datetime UTC pour HA.

    Format OPNsense : "Mon May 25 10:45:38 CEST 2026". strptime ne sait pas
    résoudre une abréviation de fuseau (CEST, EST...) : l'heure est donc lue
    sans fuseau puis interprétée dans le fuseau de Home Assistant (supposé
    identique à celui du pare-feu). Un décalage numérique (+0200) ou UTC/GMT
    est respecté tel quel.
    """
    raw = _get(data, "system_time", "boottime")
    if not isinstance(raw, str):
        return None
    parts = raw.split()
    if len(parts) != 6:
        _LOGGER.debug("Format boottime non reconnu : %s", raw)
        return None
    tz_token = parts[4]
    naive_raw = " ".join(parts[:4] + parts[5:])
    try:
        naive = datetime.strptime(naive_raw, "%a %b %d %H:%M:%S %Y")
    except ValueError:
        _LOGGER.debug("Format boottime non reconnu : %s", raw)
        return None
    if tz_token.upper() in ("UTC", "GMT", "Z"):
        return naive.replace(tzinfo=UTC)
    try:
        offset = datetime.strptime(tz_token, "%z").tzinfo
    except ValueError:
        offset = None
    if offset is not None:
        return naive.replace(tzinfo=offset).astimezone(UTC)
    return dt_util.as_utc(naive.replace(tzinfo=dt_util.get_default_time_zone()))


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
#    → débit temps réel pré-calculé par OPNsense (rate_bits_in/out en bps,
#      vus depuis l'hôte distant : cf. _TOP_KEY)
#  - traffic_totals = /api/diagnostics/traffic/interface
#    → compteurs cumulés depuis le boot (bytes received/transmitted en octets)
#
# On retrouve l'interface WAN par son device (ex: igc0) déjà identifié
# via interfacesInfo pour ne pas dépendre de la nomenclature OPNsense.


def _wan_device_name(data: dict) -> str | None:
    """Nom du device de l'interface WAN (ex: 'igc0')."""
    wan = _wan_interface(data)
    return wan.get("device") if wan else None


def _wan_rate(data: dict, direction: str) -> int | None:
    """Débit WAN, de la source la plus précise à la plus approximative.

    1. flux temps réel OPNsenseLive (moyenne exacte sur quelques secondes) ;
    2. compteurs d'octets du polling rapide (moyenne depuis le cycle précédent) ;
    3. somme instantanée des destinations (/traffic/top, polling lent).
    """
    key = f"{direction}_bps"
    live = (data.get("_live") or {}).get("wan")
    if isinstance(live, dict) and live.get(key) is not None:
        return live[key]
    rate = data.get("_wan_rate")
    if isinstance(rate, dict) and rate.get(key) is not None:
        return rate[key]
    return _top_sum_in_bps(data) if direction == "in" else _top_sum_out_bps(data)


def _traffic_in_bps(data: dict) -> int | None:
    return _wan_rate(data, "in")


def _traffic_out_bps(data: dict) -> int | None:
    return _wan_rate(data, "out")


def _cpu_usage(data: dict) -> float | None:
    """CPU utilisé en % (flux temps réel uniquement)."""
    cpu = (data.get("_live") or {}).get("cpu")
    if isinstance(cpu, dict):
        try:
            return round(float(cpu["total"]), 1)
        except (KeyError, TypeError, ValueError):
            return None
    return None


# traffic_top.py (OPNsense) agrège la sortie d'iftop par hôte DISTANT :
# rate_bits_out = ce que cet hôte envoie (ligne "<=", donc notre
# téléchargement), rate_bits_in = ce qu'il reçoit (notre envoi). On remet
# ces champs dans le sens du pare-feu.
_TOP_KEY = {"in": "rate_bits_out", "out": "rate_bits_in"}


def _top_sum_in_bps(data: dict) -> int | None:
    """Débit entrant WAN = somme des téléchargements par destination (repli).

    L'endpoint /traffic/top/wan renvoie les débits par destination dans
    une liste 'records'. Le débit global = somme des débits reçus.
    """
    records = _get(data, "traffic_wan", "wan", "records")
    if not isinstance(records, list):
        return None
    total = 0
    found = False
    for rec in records:
        if isinstance(rec, dict):
            val = rec.get(_TOP_KEY["in"])
            if val is not None:
                try:
                    total += int(val)
                    found = True
                except (TypeError, ValueError):
                    continue
    return total if found else None


def _top_sum_out_bps(data: dict) -> int | None:
    """Débit sortant WAN = somme des envois par destination (repli)."""
    records = _get(data, "traffic_wan", "wan", "records")
    if not isinstance(records, list):
        return None
    total = 0
    found = False
    for rec in records:
        if isinstance(rec, dict):
            val = rec.get(_TOP_KEY["out"])
            if val is not None:
                try:
                    total += int(val)
                    found = True
                except (TypeError, ValueError):
                    continue
    return total if found else None


def _wan_devices(data: dict) -> list[str]:
    """Devices des liens WAN (à défaut : l'interface WAN résolue)."""
    devices = [link["device"] for link in data.get("_wans") or []
               if link.get("device")]
    if devices:
        return devices
    device = _wan_device_name(data)
    return [device] if device else []


def _traffic_total(data: dict, index: int) -> int | None:
    """Total d'octets (0 = reçus, 1 = émis) cumulé sur tous les liens WAN."""
    values = [wan_counters(data, device) for device in _wan_devices(data)]
    values = [v for v in values if v is not None]
    return sum(v[index] for v in values) if values else None


def _traffic_total_received(data: dict) -> int | None:
    """Total octets reçus sur le(s) WAN depuis le dernier reset des compteurs."""
    return _traffic_total(data, 0)


def _traffic_total_transmitted(data: dict) -> int | None:
    """Total octets transmis sur le(s) WAN depuis le dernier reset des compteurs."""
    return _traffic_total(data, 1)


# ----- Top destinations WAN -----
#
# On expose 2 sensors :
#   - state = nom de la #1 destination (reverse DNS ou IP fallback)
#   - attribut "top_5" = liste des 5 plus gros consommateurs avec nom + débit
#
# Le tri se fait sur le débit reçu (ou envoyé) pour respectivement le download
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

    direction = 'in'  -> téléchargement (rate_bits_out, cf. _TOP_KEY)
    direction = 'out' -> téléversement (rate_bits_in)

    Renvoie une liste de dicts {name, address, rate_bps, rate_mbps},
    triée du plus gros au plus petit, limitée à TOP_N entrées.
    """
    records = _get(data, "traffic_wan", "wan", "records")
    if not isinstance(records, list) or not records:
        return None

    rate_key = _TOP_KEY[direction]
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


# ----- Passerelles (dpinger) -----
#
# /api/routes/gateway/status renvoie {"items": [{name, address, status,
# status_translated, delay: "3.2 ms", stddev, loss: "0.0 %", monitor}]}.
# Les valeurs numériques sont des chaînes avec unité, "~" si non mesuré.


_leading_float = leading_float


def _gateways(data: dict) -> list[dict] | None:
    """Liste normalisée des passerelles surveillées par OPNsense."""
    items = _get(data, "gateway_status", "items")
    if not isinstance(items, list):
        return None
    gateways = []
    for item in items:
        if not isinstance(item, dict):
            continue
        gateways.append(
            {
                "name": item.get("name"),
                "address": item.get("address"),
                "monitor": item.get("monitor"),
                "status": item.get("status_translated") or item.get("status"),
                "online": item.get("status") in ("none", "online"),
                "delay_ms": _leading_float(item.get("delay")),
                "loss_pct": _leading_float(item.get("loss")),
            }
        )
    return gateways


def _wan_gateway(data: dict) -> dict | None:
    """Passerelle de l'interface WAN résolue (IPv4 en priorité).

    On rapproche les adresses de passerelle portées par l'interface WAN
    (interfacesInfo) de celles surveillées par dpinger ; à défaut, première
    passerelle mesurée.
    """
    gateways = _gateways(data)
    if not gateways:
        return None
    wan = _wan_interface(data) or {}
    for address in wan.get("gateways") or []:
        for gw in gateways:
            if gw.get("address") == address and gw.get("delay_ms") is not None:
                return gw
    for gw in gateways:
        if gw.get("delay_ms") is not None:
            return gw
    return None


def _main_link(data: dict) -> dict | None:
    """Lien WAN qui porte la route par défaut (multi-WAN), sinon None."""
    return default_link(data.get("_wans") or [])


def _wan_latency(data: dict) -> float | None:
    """Latence du WAN qui porte la route par défaut."""
    link = _main_link(data)
    if link and link.get("delay_ms") is not None:
        return link["delay_ms"]
    gw = _wan_gateway(data)
    return gw.get("delay_ms") if gw else None


def _wan_packet_loss(data: dict) -> float | None:
    link = _main_link(data)
    if link and link.get("loss_pct") is not None:
        return link["loss_pct"]
    gw = _wan_gateway(data)
    return gw.get("loss_pct") if gw else None


def _wans_attribute(data: dict) -> list[dict]:
    """Résumé des liens WAN pour les attributs (dashboard, automatisations)."""
    return [
        {key: link.get(key) for key in (
            "id", "name", "device", "gateway", "online", "status", "default",
            "delay_ms", "loss_pct",
        )}
        for link in data.get("_wans") or []
    ]


# ----- Services -----
#
# /api/core/service/search renvoie {"rows": [{id, name, description,
# running: 0|1, locked}]}.


def _services(data: dict) -> list[dict] | None:
    rows = _get(data, "services", "rows")
    if not isinstance(rows, list):
        return None
    return [r for r in rows if isinstance(r, dict)]


def _services_stopped(data: dict) -> int | None:
    services = _services(data)
    if services is None:
        return None
    return sum(1 for svc in services if not int(svc.get("running") or 0))


def _services_attributes(data: dict) -> dict:
    services = _services(data) or []
    stopped = [
        svc.get("description") or svc.get("name")
        for svc in services
        if not int(svc.get("running") or 0)
    ]
    return {
        "total": len(services),
        "running": len(services) - len(stopped),
        "stopped": stopped,
    }


# ----- Tunnels VPN -----
#
# Déduits d'interfacesInfo (aucun privilège supplémentaire) : interfaces des
# groupes WireGuard / IPsec / OpenVPN.

_TUNNEL_KINDS = (
    ("wireguard", "WireGuard"),
    ("wg", "WireGuard"),
    ("ipsec", "IPsec"),
    ("openvpn", "OpenVPN"),
)


def _tunnel_kind(row: dict) -> str | None:
    groups = row.get("groups") or []
    for group, label in _TUNNEL_KINDS:
        if group in groups:
            return label
    if str(row.get("device") or "").startswith("ovpn"):
        return "OpenVPN"
    return None


def _tunnels(data: dict) -> list[dict] | None:
    """Tunnels VPN avec leur état (up/down) et leurs adresses."""
    rows = _get(data, "interfaces", "rows")
    if not isinstance(rows, list):
        return None
    tunnels = []
    for row in rows:
        if not isinstance(row, dict) or row.get("enabled") is False:
            continue
        kind = _tunnel_kind(row)
        if kind is None:
            continue
        description = row.get("description") or ""
        if not description or description == "Unassigned Interface":
            description = row.get("device") or "?"
        address = (row.get("addr4") or row.get("addr6") or "").split("/")[0]
        tunnels.append(
            {
                "name": description.removeprefix("IFACE_").replace("_", " "),
                "device": row.get("device"),
                "kind": kind,
                "up": row.get("status") == "up",
                "address": address or None,
                "remote": (row.get("tunnel") or {}).get("dest_addr"),
            }
        )
    return tunnels


# ----- Températures -----
#
# /api/diagnostics/system/system_temperature renvoie une liste de sondes
# {device, device_seq, temperature: "47.0", type: cpu|amd|zone|platform|other}.
# Les modules SFP rapportent leur propre température dans interfacesInfo
# (row["sfp"]["temperature"] = "34.39 C").

_CPU_SENSOR_TYPES = ("cpu", "amd", "intel")


def _temperatures(data: dict) -> list[dict] | None:
    """Sondes de température normalisées (valeur en °C)."""
    raw = data.get("system_temperature")
    if not isinstance(raw, list):
        return None
    sensors = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        value = _leading_float(item.get("temperature"))
        if value is None:
            continue
        sensors.append({
            "device": item.get("device"),
            "type": item.get("type") or "other",
            "label": item.get("type_translated") or item.get("type"),
            "celsius": value,
        })
    return sensors


def _temp_cpu(data: dict) -> float | None:
    """Température CPU : la plus chaude des sondes processeur (repli : toutes)."""
    sensors = _temperatures(data) or []
    cpu = [s["celsius"] for s in sensors if s["type"] in _CPU_SENSOR_TYPES]
    values = cpu or [s["celsius"] for s in sensors]
    return round(max(values), 1) if values else None


def _sfp_modules(data: dict) -> list[dict]:
    """Modules SFP qui rapportent une température (interfacesInfo)."""
    rows = _get(data, "interfaces", "rows")
    modules = []
    for row in rows if isinstance(rows, list) else []:
        sfp = row.get("sfp") if isinstance(row, dict) else None
        if isinstance(sfp, dict):
            value = _leading_float(sfp.get("temperature"))
            if value is not None:
                modules.append({
                    "device": row.get("device"),
                    "interface": (row.get("description") or "").removeprefix("IFACE_"),
                    "module": (sfp.get("plugged") or "").strip(),
                    "celsius": value,
                })
    return modules


def _temp_sfp(data: dict) -> float | None:
    modules = _sfp_modules(data)
    return round(max(m["celsius"] for m in modules), 1) if modules else None


def _vpn_tunnels_up(data: dict) -> int | None:
    tunnels = _tunnels(data)
    if tunnels is None:
        return None
    return sum(1 for tunnel in tunnels if tunnel["up"])


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
            key="cpu_usage",
            translation_key="cpu_usage",
            icon="mdi:cpu-64-bit",
            native_unit_of_measurement=PERCENTAGE,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=1,
        ),
        _cpu_usage,
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
    # ----- Qualité de la connexion (privilège "Status: Gateways") -----
    (
        SensorEntityDescription(
            key="wan_latency",
            translation_key="wan_latency",
            icon="mdi:timer-outline",
            device_class=SensorDeviceClass.DURATION,
            native_unit_of_measurement=UnitOfTime.MILLISECONDS,
            suggested_display_precision=1,
            state_class=SensorStateClass.MEASUREMENT,
        ),
        _wan_latency,
    ),
    (
        SensorEntityDescription(
            key="wan_packet_loss",
            translation_key="wan_packet_loss",
            icon="mdi:package-variant-remove",
            native_unit_of_measurement=PERCENTAGE,
            suggested_display_precision=1,
            state_class=SensorStateClass.MEASUREMENT,
        ),
        _wan_packet_loss,
    ),
    # ----- Services (privilège "Status: Services") -----
    (
        SensorEntityDescription(
            key="services_stopped",
            translation_key="services_stopped",
            icon="mdi:cog-stop-outline",
            state_class=SensorStateClass.MEASUREMENT,
        ),
        _services_stopped,
    ),
    # ----- Températures (même contrôleur que system_information) -----
    (
        SensorEntityDescription(
            key="temp_cpu",
            translation_key="temp_cpu",
            icon="mdi:thermometer",
            device_class=SensorDeviceClass.TEMPERATURE,
            native_unit_of_measurement=UnitOfTemperature.CELSIUS,
            suggested_display_precision=1,
            state_class=SensorStateClass.MEASUREMENT,
        ),
        _temp_cpu,
    ),
    (
        SensorEntityDescription(
            key="temp_sfp",
            translation_key="temp_sfp",
            icon="mdi:expansion-card-variant",
            device_class=SensorDeviceClass.TEMPERATURE,
            native_unit_of_measurement=UnitOfTemperature.CELSIUS,
            suggested_display_precision=1,
            state_class=SensorStateClass.MEASUREMENT,
        ),
        _temp_sfp,
    ),
    # ----- Tunnels VPN (déduits des interfaces) -----
    (
        SensorEntityDescription(
            key="vpn_tunnels_up",
            translation_key="vpn_tunnels_up",
            icon="mdi:vpn",
            state_class=SensorStateClass.MEASUREMENT,
        ),
        _vpn_tunnels_up,
    ),
)


# Mapping sensor_key -> fonction qui extrait les attributs supplémentaires.
# Permet d'exposer top_5 sur les sensors top_dest sans toucher aux autres.
ATTRIBUTE_EXTRACTORS = {
    "wan_top_dest_in": lambda data: {"top_5": _top_destinations(data, "in") or []},
    "wan_top_dest_out": lambda data: {"top_5": _top_destinations(data, "out") or []},
    "wan_latency": lambda data: {
        "gateway": (_main_link(data) or {}).get("gateway")
        or (_wan_gateway(data) or {}).get("name"),
        "wans": _wans_attribute(data),
        "gateways": _gateways(data) or [],
    },
    "services_stopped": _services_attributes,
    "temp_cpu": lambda data: {"sensors": _temperatures(data) or []},
    "temp_sfp": lambda data: {"modules": _sfp_modules(data)},
    "vpn_tunnels_up": lambda data: {
        "total": len(_tunnels(data) or []),
        "tunnels": _tunnels(data) or [],
    },
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Crée tous les sensors à partir des descriptions ci-dessus."""
    hub: OPNsenseDataCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = [
        OPNsenseSensor(hub, entry, description, value_fn)
        for description, value_fn in SENSOR_DESCRIPTIONS
    ]
    async_add_entities(entities)

    # Multi-WAN : capteurs par lien et par groupe de passerelles, créés au
    # démarrage puis dès qu'un nouveau lien / groupe apparaît.
    known: set[str] = set()

    def _sync_dynamic() -> None:
        data = hub.fast.data or {}
        new: list[SensorEntity] = []
        for link in links_with_entities(data):
            if f"link:{link['id']}" in known:
                continue
            known.add(f"link:{link['id']}")
            new += [
                OPNsenseLinkSensor(hub, entry, link, description, value_fn)
                for description, value_fn in LINK_SENSOR_DESCRIPTIONS
            ]
        for group in data.get("_wan_groups") or []:
            if not group.get("name") or f"group:{group['name']}" in known:
                continue
            known.add(f"group:{group['name']}")
            new.append(OPNsenseGatewayGroupSensor(hub, entry, group["name"]))
        if new:
            async_add_entities(new)

    _sync_dynamic()
    entry.async_on_unload(hub.fast.async_add_listener(_sync_dynamic))


# Rythme de rafraîchissement de chaque capteur (les autres : polling lent).
FAST_SENSORS = {
    "public_ipv4", "public_ipv6", "wan_status", "wan_total_received",
    "wan_total_transmitted", "wan_latency", "wan_packet_loss", "vpn_tunnels_up",
    "temp_cpu", "temp_sfp",
}
# Temps réel : flux OPNsense, avec repli sur le polling rapide puis lent.
LIVE_SENSORS = {"wan_throughput_in", "wan_throughput_out", "cpu_usage"}


def tier_coordinators(hub: OPNsenseDataCoordinator, key: str) -> list:
    """Coordinators à écouter pour une entité (le premier est le principal)."""
    if key in LIVE_SENSORS:
        return [c for c in (hub.live, hub.fast, hub) if c is not None]
    if key in FAST_SENSORS:
        return [hub.fast]
    return [hub]


class OPNsenseSensor(CoordinatorEntity, SensorEntity):
    """Sensor générique : lit la vue fusionnée, écoute son rythme de refresh."""

    _attr_has_entity_name = True

    def __init__(
        self,
        hub: OPNsenseDataCoordinator,
        entry: ConfigEntry,
        description: SensorEntityDescription,
        value_fn: Callable[[dict], Any],
        coordinators: list | None = None,
    ) -> None:
        """Initialise le sensor."""
        coordinators = coordinators or tier_coordinators(hub, description.key)
        super().__init__(coordinators[0])
        self._hub = hub
        self._extra_coordinators = coordinators[1:]
        self.entity_description = description
        self._value_fn = value_fn
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        # Toutes les entités rattachées au même appareil firewall
        self._attr_device_info = build_device_info(entry, hub.data)

    async def async_added_to_hass(self) -> None:
        """Écoute aussi les coordinators de repli (temps réel -> rapide -> lent)."""
        await super().async_added_to_hass()
        for coordinator in self._extra_coordinators:
            self.async_on_remove(
                coordinator.async_add_listener(self._handle_coordinator_update)
            )

    @property
    def available(self) -> bool:
        """Disponible si au moins une de ses sources répond."""
        return any(
            c.last_update_success
            for c in (self.coordinator, *self._extra_coordinators)
        )

    @property
    def native_value(self) -> Any:
        """Lit la valeur depuis la vue fusionnée des coordinators."""
        return self._value_fn(self._hub.merged)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose des attributs supplémentaires pour certains sensors.

        Utilisé notamment pour wan_top_dest_in/out qui exposent un top_5
        complet sous forme de liste consultable depuis Lovelace.
        """
        extractor = ATTRIBUTE_EXTRACTORS.get(self.entity_description.key)
        if extractor is None:
            return None
        try:
            return extractor(self._hub.merged)
        except Exception:  # noqa: BLE001
            return None


# ============================================================
#  Multi-WAN : un jeu de capteurs par lien, un capteur par groupe
# ============================================================


def _link_rate(data: dict, link_id: str, direction: str) -> int | None:
    """Débit d'un lien : flux temps réel, sinon compteurs du polling rapide."""
    key = f"{direction}_bps"
    for source in ((data.get("_live") or {}).get("wans") or {},
                   data.get("_wan_rates") or {}):
        rate = source.get(link_id)
        if isinstance(rate, dict) and rate.get(key) is not None:
            return rate[key]
    return None


def _link_field(field: str) -> Callable[[dict, str], Any]:
    """value_fn lisant un champ du lien (latence, pertes...)."""
    def value(data: dict, link_id: str) -> Any:
        return (find_link(data, link_id) or {}).get(field)
    return value


_RATE = {
    "device_class": SensorDeviceClass.DATA_RATE,
    "native_unit_of_measurement": UnitOfDataRate.BITS_PER_SECOND,
    "suggested_unit_of_measurement": UnitOfDataRate.MEGABITS_PER_SECOND,
    "suggested_display_precision": 2,
    "state_class": SensorStateClass.MEASUREMENT,
}

LINK_SENSOR_DESCRIPTIONS: tuple[
    tuple[SensorEntityDescription, Callable[[dict, str], Any]], ...
] = (
    (
        SensorEntityDescription(
            key="latency",
            translation_key="link_latency",
            icon="mdi:timer-outline",
            device_class=SensorDeviceClass.DURATION,
            native_unit_of_measurement=UnitOfTime.MILLISECONDS,
            suggested_display_precision=1,
            state_class=SensorStateClass.MEASUREMENT,
        ),
        _link_field("delay_ms"),
    ),
    (
        SensorEntityDescription(
            key="packet_loss",
            translation_key="link_packet_loss",
            icon="mdi:package-variant-remove",
            native_unit_of_measurement=PERCENTAGE,
            suggested_display_precision=1,
            state_class=SensorStateClass.MEASUREMENT,
        ),
        _link_field("loss_pct"),
    ),
    (
        SensorEntityDescription(
            key="throughput_in", translation_key="link_throughput_in",
            icon="mdi:download-network", **_RATE,
        ),
        lambda data, link_id: _link_rate(data, link_id, "in"),
    ),
    (
        SensorEntityDescription(
            key="throughput_out", translation_key="link_throughput_out",
            icon="mdi:upload-network", **_RATE,
        ),
        lambda data, link_id: _link_rate(data, link_id, "out"),
    ),
)


class OPNsenseLinkSensor(OPNsenseSensor):
    """Capteur d'un lien WAN donné (latence, pertes, débits)."""

    def __init__(
        self,
        hub: OPNsenseDataCoordinator,
        entry: ConfigEntry,
        link: dict,
        description: SensorEntityDescription,
        value_fn: Callable[[dict, str], Any],
    ) -> None:
        """Initialise ; `link` = le lien tel qu'il était à la création."""
        link_id = link["id"]
        self._link_id = link_id
        rate = description.key.startswith("throughput")
        super().__init__(
            hub, entry, description,
            lambda data: value_fn(data, link_id),
            coordinators=(
                [c for c in (hub.live, hub.fast) if c is not None]
                if rate else [hub.fast]
            ),
        )
        self._attr_unique_id = f"{entry.entry_id}_wan_{link_id}_{description.key}"
        self._attr_translation_placeholders = {"wan": link["name"]}

    @property
    def available(self) -> bool:
        """Indisponible si le lien a disparu (passerelle supprimée, exclue)."""
        return super().available and find_link(
            self._hub.merged, self._link_id
        ) is not None


class OPNsenseGatewayGroupSensor(OPNsenseSensor):
    """Groupe de passerelles : état = passerelle(s) qui portent le trafic."""

    def __init__(
        self, hub: OPNsenseDataCoordinator, entry: ConfigEntry, name: str
    ) -> None:
        """Initialise."""
        self._group = name
        super().__init__(
            hub, entry,
            SensorEntityDescription(
                key=f"gateway_group_{name}",
                translation_key="gateway_group",
                icon="mdi:router-network",
            ),
            self._state,
            coordinators=[hub.fast],
        )
        self._attr_translation_placeholders = {"group": name}

    def _state(self, data: dict) -> str | None:
        group = find_group(data, self._group)
        if group is None:
            return None
        return " + ".join(group["active"]) if group["active"] else "hors ligne"

    @property
    def available(self) -> bool:
        """Indisponible si le groupe a été supprimé côté OPNsense."""
        return super().available and find_group(
            self._hub.merged, self._group
        ) is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Membres par niveau de priorité, avec leur état."""
        group = find_group(self._hub.merged, self._group)
        if group is None:
            return None
        return {key: group[key] for key in (
            "description", "trigger", "active", "members_usable",
            "members_total", "tiers",
        )}
