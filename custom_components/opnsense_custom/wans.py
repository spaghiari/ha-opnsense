"""Liens WAN et groupes de passerelles (multi-WAN).

Un WAN = une interface portant au moins une passerelle « montante »
(case « Upstream Gateway » d'OPNsense). Les passerelles VPN (Proton,
IPsec...) n'ont pas cette case : elles ne sont jamais prises pour un WAN.
Les passerelles IPv4 et IPv6 d'une même interface forment un seul lien.

Sources (polling rapide) :
  * /api/routing/settings/search_gateway : configuration (interface,
    protocole, upstream, défaut actif, priorité) - privilège « System:
    Gateways », le même que la latence ;
  * /api/routes/gateway/status : état brut mesuré par dpinger (`status`
    = none / down / loss / delay..., non traduit) ;
  * /api/routing/group_settings/search : groupes de passerelles (OPNsense
    récent, privilège « System: Gateway Groups ») - optionnel.

Un lien est coupé si son interface est down, ou si sa passerelle
principale est déclarée down par dpinger ou perd 100 % des paquets : une
panne côté opérateur (lien physique up, plus rien ne passe) est détectée.

Sans `search_gateway` (privilège absent, OPNsense ancien), on retombe sur
un lien unique construit depuis l'interface WAN résolue (comportement v2.2).
"""
from __future__ import annotations

from typing import Any


def leading_float(raw: Any) -> float | None:
    """Extrait le nombre en tête d'une chaîne type "3.2 ms" / "0.0 %"."""
    if isinstance(raw, int | float) and not isinstance(raw, bool):
        return float(raw)
    if not isinstance(raw, str):
        return None
    token = raw.strip().split(" ")[0]
    try:
        return float(token)
    except ValueError:
        return None


def _truthy(value: Any) -> bool:
    """Booléen OPNsense : True, "1", 1."""
    return value is True or value in ("1", 1)


def _rows(data: dict, key: str) -> list[dict]:
    block = data.get(key)
    rows = block.get("rows") if isinstance(block, dict) else None
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def gateway_states(data: dict) -> dict[str, dict]:
    """État dpinger brut indexé par nom de passerelle."""
    block = data.get("gateway_status")
    items = block.get("items") if isinstance(block, dict) else None
    states: dict[str, dict] = {}
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and item.get("name"):
            states[item["name"]] = item
    return states


def gateway_health(state: dict | None) -> dict[str, Any]:
    """Normalise un état dpinger : online, statut, latence, pertes."""
    state = state or {}
    raw = str(state.get("status") or "")
    loss = leading_float(state.get("loss"))
    down = "down" in raw or (loss is not None and loss >= 100)
    return {
        "online": not down if raw or loss is not None else None,
        "status": state.get("status_translated") or raw or None,
        "degraded": not down and ("loss" in raw or "delay" in raw),
        "delay_ms": leading_float(state.get("delay")),
        "loss_pct": loss,
    }


def _interface_rows(data: dict) -> list[dict]:
    rows = (data.get("interfaces") or {}).get("rows")
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _row_for(data: dict, identifier: str | None, device: str | None) -> dict:
    for row in _interface_rows(data):
        if device and row.get("device") == device:
            return row
        if identifier and row.get("identifier") == identifier:
            return row
    return {}


def wan_links(data: dict, exclude: list[str] | tuple[str, ...] = ()) -> list[dict]:
    """Liens WAN détectés, triés par priorité (le lien par défaut d'abord).

    Chaque lien : id (identifiant d'interface OPNsense, ex. "wan", "opt2"),
    name, device, gateways (noms), gateway (principale), online, status,
    degraded, delay_ms, loss_pct, default (porte la route par défaut).
    """
    configured = _rows(data, "gateways")
    if not configured:
        return _fallback_link(data)

    states = gateway_states(data)
    links: dict[str, dict] = {}
    for gw in configured:
        if not _truthy(gw.get("upstream")) or _truthy(gw.get("disabled")):
            continue
        ident = gw.get("interface")
        if not ident or ident in exclude:
            continue
        link = links.setdefault(ident, {
            "id": ident,
            "name": (gw.get("interface_descr") or str(ident).upper())
            .removeprefix("IFACE_"),
            "device": gw.get("if"),
            "gateways": [],
            "_v4": None, "_v6": None,
            "default": False,
            "priority": None,
        })
        link["gateways"].append(gw.get("name"))
        family = "_v6" if gw.get("ipprotocol") == "inet6" else "_v4"
        if link[family] is None:
            link[family] = gw
        if _truthy(gw.get("defaultgw")):
            link["default"] = True
        prio = leading_float(gw.get("priority"))
        if prio is not None and (link["priority"] is None or prio < link["priority"]):
            link["priority"] = prio

    result = []
    for link in links.values():
        primary = link.pop("_v4") or link.pop("_v6")
        link.pop("_v6", None)
        row = _row_for(data, link["id"], link["device"])
        link["device"] = link["device"] or row.get("device")
        health = gateway_health(states.get(primary.get("name")))
        if row and row.get("status") not in (None, "up"):
            # Câble débranché / interface désactivée : coupé quoi que dise dpinger.
            health["online"] = False
        link.update(gateway=primary.get("name"), **health)
        result.append(link)
    result.sort(key=lambda link: (
        not link["default"],
        link["priority"] if link["priority"] is not None else 255,
        link["name"],
    ))
    return result


def _fallback_link(data: dict) -> list[dict]:
    """Lien unique depuis l'interface WAN résolue (sans search_gateway)."""
    target = data.get("_wan_device")
    row = next(
        (r for r in _interface_rows(data) if target and r.get("device") == target),
        None,
    )
    if row is None:
        return []
    states = gateway_states(data)
    primary = None
    for address in row.get("gateways") or []:
        primary = next(
            (s for s in states.values() if s.get("address") == address), None
        )
        if primary:
            break
    health = gateway_health(primary) if primary else {
        "online": None, "status": None, "degraded": False,
        "delay_ms": None, "loss_pct": None,
    }
    if row.get("status") != "up":
        health["online"] = False
    elif health["online"] is None:
        health["online"] = True
    return [{
        "id": row.get("identifier") or "wan",
        "name": (row.get("description") or "WAN").removeprefix("IFACE_"),
        "device": row.get("device"),
        "gateways": [primary.get("name")] if primary else [],
        "gateway": primary.get("name") if primary else None,
        "default": True,
        "priority": None,
        **health,
    }]


def default_link(links: list[dict]) -> dict | None:
    """Lien qui porte la route par défaut (à défaut : le premier en ligne)."""
    for link in links:
        if link["default"]:
            return link
    for link in links:
        if link["online"]:
            return link
    return links[0] if links else None


def internet_up(links: list[dict]) -> bool | None:
    """True si au moins un lien WAN est en ligne."""
    known = [link["online"] for link in links if link["online"] is not None]
    if not known:
        return None
    return any(known)


# ----- Groupes de passerelles -----
#
# search renvoie {"rows": [{uuid, name, trigger, descr, gateways: {tier:
# [état dpinger + label]}}]}. Le trafic passe par le niveau (tier) de plus
# petit numéro qui a au moins un membre utilisable ; plusieurs membres sur ce
# niveau = répartition de charge.

_TRIGGER_FAILS = {
    "down": ("down",),
    "downloss": ("down", "loss"),
    "downlatency": ("down", "delay"),
    "downlosslatency": ("down", "loss", "delay"),
}


def _member_usable(member: dict, trigger: str) -> bool:
    raw = str(member.get("status") or "")
    if not raw:
        return False  # désactivée ou inactive
    fails = _TRIGGER_FAILS.get(trigger, ("down",))
    if any(word in raw for word in fails):
        return False
    loss = leading_float(member.get("loss"))
    return not (loss is not None and loss >= 100)


def gateway_groups(data: dict) -> list[dict] | None:
    """Groupes de passerelles normalisés, ou None si l'API est absente."""
    if not isinstance(data.get("gateway_groups"), dict):
        return None
    groups = []
    for row in _rows(data, "gateway_groups"):
        trigger = str(row.get("trigger") or "down")
        raw_tiers = row.get("gateways") or {}
        if isinstance(raw_tiers, list):
            raw_tiers = dict(enumerate(raw_tiers, start=1))
        tiers = []
        for tier_key in sorted(raw_tiers, key=lambda k: leading_float(k) or 0):
            members = []
            for member in raw_tiers[tier_key] or []:
                if not isinstance(member, dict):
                    continue
                health = gateway_health(member)
                members.append({
                    "name": member.get("name"),
                    "usable": _member_usable(member, trigger),
                    "status": health["status"],
                    "delay_ms": health["delay_ms"],
                    "loss_pct": health["loss_pct"],
                })
            if members:
                tiers.append({"tier": int(leading_float(tier_key) or 0),
                              "gateways": members})
        active: list[str] = []
        for tier in tiers:
            active = [m["name"] for m in tier["gateways"] if m["usable"]]
            if active:
                break
        all_members = [m for tier in tiers for m in tier["gateways"]]
        groups.append({
            "name": row.get("name"),
            "description": row.get("descr") or None,
            "trigger": trigger,
            "active": active,
            "members_usable": sum(1 for m in all_members if m["usable"]),
            "members_total": len(all_members),
            "tiers": tiers,
        })
    return groups


def links_with_entities(data: dict) -> list[dict]:
    """Liens qui méritent leurs propres entités : seulement en multi-WAN.

    Avec un seul WAN, les entités globales (WAN connecté, latence, débit...)
    décrivent déjà ce lien : on n'en crée pas de doublons.
    """
    links = data.get("_wans") or []
    return links if len(links) >= 2 else []


def find_link(data: dict, link_id: str) -> dict | None:
    """Lien WAN par identifiant (None s'il a disparu ou est exclu)."""
    for link in data.get("_wans") or []:
        if link.get("id") == link_id:
            return link
    return None


def find_group(data: dict, name: str) -> dict | None:
    """Groupe de passerelles par nom (None s'il a disparu)."""
    for group in data.get("_wan_groups") or []:
        if group.get("name") == name:
            return group
    return None
