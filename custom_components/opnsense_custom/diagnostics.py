"""Diagnostics téléchargeables pour OPNsense custom.

Permet d'exporter l'état de l'intégration (config + dernier snapshot du
coordinator) depuis l'UI HA, avec masquage automatique des secrets et de la
topologie réseau (IP, MAC, hostname, noms DNS).
"""
from __future__ import annotations

import re
import string
from typing import Any

from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_API_KEY,
    CONF_API_SECRET,
    CONF_HOST,
    CONF_NOTIFY_TARGETS,
    DOMAIN,
)
from .coordinator import OPNsenseDataCoordinator

# Secrets de la config : jamais exportés en clair.
TO_REDACT_ENTRY = {CONF_API_KEY, CONF_API_SECRET, CONF_HOST}

# Options sensibles : notify_targets liste les services notify.mobile_app_*
# (noms des téléphones des utilisateurs), cf. alerts.py:127.
TO_REDACT_OPTIONS = {CONF_NOTIFY_TARGETS}

# Clés qui portent une IP, une MAC, un nom DNS ou un identifiant d'appareil,
# masquées à toute profondeur (async_redact_data est récursif). Filet de
# sécurité : les blocs bruts de l'API passent d'abord par une liste blanche
# (_sanitize), ce qui couvre aussi les champs que le code ne lit pas.
#   addr4 / addr6 / ipaddr : IP des interfaces, dont l'IP publique WAN
#       (sensor.py:289-299, coordinator.py:81) ;
#   macaddr : MAC des interfaces (réponse brute interfacesInfo) ;
#   address / monitor : IP des passerelles dpinger et des destinations du top
#       (sensor.py:512, sensor.py:556-557, wans.py:198) ;
#   rname : reverse DNS des destinations (sensor.py:473) ;
#   dest_addr / src_addr : extrémités d'un tunnel VPN (sensor.py:701) ;
#   routes : routes de l'interface (réponse brute) ;
#   uuid : identifiant des groupes / passerelles (wans.py:243).
# "name" n'est volontairement PAS dans la liste : il porte aussi les noms de
# passerelles, groupes et services (wans.py:59, sensor.py:644), utiles au
# débogage. Le hostname (system_information.name, sensor.py:67) est masqué
# par un traitement ciblé dans _sanitize().
TO_REDACT_DATA = {
    "addr4", "addr6", "ipaddr", "macaddr", "address", "monitor", "rname",
    "dest_addr", "src_addr", "routes", "uuid",
}

# Listes blanches des blocs bruts de l'API : seuls les champs utiles au
# débogage (et lus par le code) sont exportés, tout le reste est écarté
# (macaddr_hw, ifctl, config, alias, dhcphostname, spoofmac...).
_IFACE_KEEP = (
    "identifier", "device", "description", "status", "enabled", "link_type",
    "mtu", "media", "is_physical", "vlan_tag", "statistics", "groups",
)
_GATEWAY_KEEP = (
    "name", "interface", "interface_descr", "if", "ipprotocol", "upstream",
    "defaultgw", "disabled", "priority", "descr",
)
_GW_STATE_KEEP = (
    "name", "status", "status_translated", "delay", "stddev", "loss",
)
_GROUP_KEEP = ("name", "trigger", "descr")

# Clés de texte libre saisi par l'utilisateur : filtrées par _mask_text.
_FREE_TEXT_KEYS = {"descr", "description", "interface_descr", "name"}

# Compromis : un texte libre peut contenir une IP ou une MAC ; on masque les
# IPv4/IPv6/MAC littérales mais pas les noms DNS ni le reste du libellé, qui
# reste utile au débogage.
_IPV4_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_IPV6_RE = re.compile(
    r"(?<![\w:])(?:[0-9a-fA-F]{0,4}:){2,7}[0-9a-fA-F]{0,4}(?![\w:])"
)
_MAC_RE = re.compile(r"\b[0-9a-fA-F]{2}(?:[:-][0-9a-fA-F]{2}){5}\b")
# Marqueurs propres aux erreurs de certificat (le CN / SAN y figure)
_CERT_ERROR_RE = re.compile(
    r"certificate|CERTIFICATE_VERIFY_FAILED|\[SSL|SSLError|hostname .* doesn't match",
    re.IGNORECASE,
)

# Clés de débit d'un enregistrement du top destinations (sensor.py:383).
_RATE_KEYS = ("rate_bits_in", "rate_bits_out")


def _mask_ipv6(match: re.Match[str]) -> str:
    """Ne masque que ce qui ressemble vraiment à une IPv6 (pas « 12:34:56 »)."""
    value = match.group(0)
    if not any(c in string.hexdigits for c in value):
        return value  # « :: » seul
    if (
        "::" in value
        or value.count(":") >= 3
        or any(c in "abcdefABCDEF" for c in value)
    ):
        return REDACTED
    return value


def _mask_text(text: str) -> str:
    """Masque les MAC, IPv4 et IPv6 littérales d'une chaîne."""
    for pattern in (_MAC_RE, _IPV4_RE):
        text = pattern.sub(REDACTED, text)
    return _IPV6_RE.sub(_mask_ipv6, text)


def _mask_free_text(value: Any) -> Any:
    """Applique _mask_text aux champs de texte libre, à toute profondeur."""
    if isinstance(value, dict):
        return {
            k: _mask_text(v) if k in _FREE_TEXT_KEYS and isinstance(v, str)
            else _mask_free_text(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_mask_free_text(v) for v in value]
    return value


def _pick(row: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    """Sous-ensemble des clés présentes dans `row`."""
    if not isinstance(row, dict):
        return {}
    return {k: row[k] for k in keys if k in row}


def _map_rows(block: Any, field: str, fn: Any) -> Any:
    """Copie de `block` dont la liste `block[field]` est transformée par `fn`."""
    if not isinstance(block, dict) or not isinstance(block.get(field), list):
        return block
    return {**block, field: [fn(r) for r in block[field] if isinstance(r, dict)]}


def _iface_row(row: dict) -> dict[str, Any]:
    """Ligne d'interface : liste blanche + indicateurs sans valeur identifiante."""
    out = _pick(row, _IFACE_KEEP)
    gateways = row.get("gateways")
    out["gateways"] = len(gateways) if isinstance(gateways, list) else 0
    out["has_addr4"] = bool(row.get("addr4"))
    out["has_addr6"] = bool(row.get("addr6"))
    out["has_tunnel"] = bool(row.get("tunnel"))
    sfp = row.get("sfp")
    if isinstance(sfp, dict) and "temperature" in sfp:
        out["sfp"] = {"temperature": sfp["temperature"]}
    return out


def _group_members(items: Any) -> Any:
    """Membres d'un niveau de groupe réduits à l'état dpinger."""
    if not isinstance(items, list):
        return items
    return [_pick(m, _GW_STATE_KEEP) for m in items if isinstance(m, dict)]


def _group_row(row: dict) -> dict[str, Any]:
    """Groupe de passerelles brut : membres réduits à l'état dpinger."""
    out = _pick(row, _GROUP_KEEP)
    tiers = row.get("gateways")
    if isinstance(tiers, dict):
        out["gateways"] = {k: _group_members(v) for k, v in tiers.items()}
    elif isinstance(tiers, list):
        out["gateways"] = _group_members(tiers)
    return out


def _top_summary(traffic: Any) -> Any:
    """Résumé non identifiant du top destinations (traffic_wan).

    On ne garde que des compteurs : nombre d'entrées, entrées locales et
    débits agrégés. Aucune IP, aucun nom DNS, aucun identifiant d'interface.
    """
    if not isinstance(traffic, dict):
        return traffic
    records: list[dict] = []
    for block in traffic.values():
        rows = block.get("records") if isinstance(block, dict) else None
        if isinstance(rows, list):
            records.extend(r for r in rows if isinstance(r, dict))
    summary: dict[str, Any] = {
        "records": len(records),
        "records_local": sum(
            1 for r in records
            if isinstance(r.get("tags"), list) and "local" in r["tags"]
        ),
    }
    for key in _RATE_KEYS:
        total = 0
        for rec in records:
            try:
                total += int(float(rec.get(key, 0)))
            except (TypeError, ValueError):
                continue
        summary[f"{key}_total"] = total
    return summary


def _sanitize(data: dict[str, Any]) -> dict[str, Any]:
    """Copie du snapshot réduite aux champs utiles (listes blanches)."""
    out = dict(data)

    # Hostname : seul "name" de system_information est masqué (le "name" des
    # passerelles, groupes et services reste visible).
    info = out.get("system_information")
    if isinstance(info, dict) and "name" in info:
        out["system_information"] = {**info, "name": REDACTED}

    if "interfaces" in out:
        out["interfaces"] = _map_rows(out["interfaces"], "rows", _iface_row)
    if "gateways" in out:
        out["gateways"] = _map_rows(
            out["gateways"], "rows", lambda r: _pick(r, _GATEWAY_KEEP)
        )
    if "gateway_status" in out:
        out["gateway_status"] = _map_rows(
            out["gateway_status"], "items", lambda r: _pick(r, _GW_STATE_KEEP)
        )
    if "gateway_groups" in out:
        out["gateway_groups"] = _map_rows(out["gateway_groups"], "rows", _group_row)

    # Top destinations : IP LAN des clients, IP et DNS distants -> résumé.
    if "traffic_wan" in out:
        out["traffic_wan"] = _top_summary(out["traffic_wan"])
    return out


def _scrub_error(text: str, host: str | None) -> str:
    """Nettoie un message d'erreur réseau : hôte, IP, certificat TLS."""
    # Pas de simple recherche de « ssl » : aiohttp écrit « ssl:default » dans
    # toutes les erreurs de connexion, qui doivent rester lisibles.
    if _CERT_ERROR_RE.search(text):
        # Le CN / SAN du certificat serait exporté : on n'en garde que l'indication.
        return "erreur TLS / certificat (détail masqué)"
    if host:
        text = re.sub(re.escape(host), REDACTED, text, flags=re.IGNORECASE)
    return _mask_text(text)


def _scrub_errors(value: Any, host: str | None) -> Any:
    """Applique _scrub_error aux champs "error" du diagnostic temps réel."""
    if isinstance(value, dict):
        return {
            k: _scrub_error(v, host) if k == "error" and isinstance(v, str)
            else _scrub_errors(v, host)
            for k, v in value.items()
        }
    return value


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Renvoie les diagnostics d'une ConfigEntry (secrets et topologie masqués)."""
    result: dict[str, Any] = {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT_ENTRY),
            "options": async_redact_data(dict(entry.options), TO_REDACT_OPTIONS),
        },
    }

    # Entrée non chargée (échec de setup, rechargement) : config masquée seule.
    coordinator: OPNsenseDataCoordinator | None = hass.data.get(DOMAIN, {}).get(
        entry.entry_id
    )
    if coordinator is None:
        result["loaded"] = False
        return result

    host = entry.data.get(CONF_HOST)
    fast = coordinator.fast.data or {}
    result.update(
        {
            "loaded": True,
            "resolved_wan_device": fast.get("_wan_device"),
            "wan_identifier": coordinator.fast.wan_identifier,
            "wan_links": _mask_free_text(
                async_redact_data(fast.get("_wans"), TO_REDACT_DATA)
            ),
            "gateway_groups": _mask_free_text(
                async_redact_data(fast.get("_wan_groups"), TO_REDACT_DATA)
            ),
            "forbidden_endpoints": sorted(coordinator.client.forbidden),
            "refresh": {
                "slow_interval_s": coordinator.update_interval.total_seconds(),
                "fast_interval_s": coordinator.fast.update_interval.total_seconds(),
                "realtime": _scrub_errors(
                    coordinator.live.diagnostics() if coordinator.live
                    else "désactivé",
                    host,
                ),
            },
            "coordinator_data": _mask_free_text(
                async_redact_data(
                    _sanitize({**(coordinator.data or {}), **fast}),
                    TO_REDACT_DATA,
                )
            ),
        }
    )
    return result
