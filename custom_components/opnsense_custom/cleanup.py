"""Nettoyage prudent des entités dynamiques (liens WAN, groupes de passerelles).

Les entités par lien et par groupe sont créées à l'apparition du lien / groupe
(sensor.py, binary_sensor.py) mais ne disparaissaient jamais : groupe supprimé
ou renommé, WAN exclu, passage de 2 WAN à 1 laissaient des entités
« indisponibles » pour toujours.

`async_setup_pruning` écoute le polling rapide et retire du registre les
entités dont le lien / groupe est absent de façon FIABLE et DURABLE :

  * jamais sur un polling en échec, ni quand les données de passerelles sont
    invalides (None, privilège manquant, repli `_fallback_link`) ;
  * seulement après `PRUNE_CYCLES` cycles réussis consécutifs d'absence ET
    `PRUNE_MIN_SECONDS` secondes ; toute réapparition ou tout cycle douteux
    remet le compteur à zéro (compteurs en mémoire).

Association entrée de registre -> lien / groupe : par `translation_key` de
l'entrée (pas d'analyse « à l'aveugle » de l'unique_id, car
`{eid}_wan_packet_loss` global et `{eid}_wan_{lien}_packet_loss` se
confondraient), puis l'identifiant est extrait de l'unique_id construit par
les entités :

  * liens (sensor.py, binary_sensor.py) :
    `{entry_id}_wan_{id_du_lien}_{suffixe}`, suffixe = clé de la description
    (latency, packet_loss, throughput_in/out, total_received/transmitted,
    connected) ;
  * groupes (sensor.py) : `{entry_id}_gateway_group_{nom}`.

À la suppression, la clé correspondante est retirée de
`hub.dynamic_known[<plateforme>]` pour que `_sync_*` recrée les entités si le
lien / groupe réapparaît.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from time import monotonic
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .coordinator import OPNsenseDataCoordinator
from .wans import links_with_entities

_LOGGER = logging.getLogger(__name__)

# Absence minimale avant suppression : cycles réussis consécutifs ET durée.
PRUNE_CYCLES = 10
# 1 h : une maintenance courte (passerelle désactivée) ne fait pas perdre les
# personnalisations des entités (nom, pièce, entity_id renommé).
PRUNE_MIN_SECONDS = 3600

# translation_key des entités par lien -> (plateforme, suffixe d'unique_id)
_LINK_ENTITIES: dict[str, tuple[str, str]] = {
    "link_latency": ("sensor", "latency"),
    "link_packet_loss": ("sensor", "packet_loss"),
    "link_throughput_in": ("sensor", "throughput_in"),
    "link_throughput_out": ("sensor", "throughput_out"),
    "link_total_received": ("sensor", "total_received"),
    "link_total_transmitted": ("sensor", "total_transmitted"),
    "link_connected": ("binary_sensor", "connected"),
}

# Cible = (plateforme, "link" | "group", identifiant du lien / nom du groupe)
_Target = tuple[str, str, str]


def _classify(entry_id: str, reg: er.RegistryEntry) -> _Target | None:
    """Cible (lien / groupe) d'une entrée de registre, None si hors périmètre."""
    if reg.platform != DOMAIN or not reg.unique_id:
        return None
    uid = reg.unique_id
    key = reg.translation_key
    if key == "gateway_group":
        prefix = f"{entry_id}_gateway_group_"
        if reg.domain == "sensor" and uid.startswith(prefix) and len(uid) > len(prefix):
            return ("sensor", "group", uid[len(prefix):])
        return None
    if key in _LINK_ENTITIES:
        platform, suffix = _LINK_ENTITIES[key]
        prefix, tail = f"{entry_id}_wan_", f"_{suffix}"
        if (reg.domain == platform and uid.startswith(prefix)
                and uid.endswith(tail) and len(uid) > len(prefix) + len(tail)):
            return (platform, "link", uid[len(prefix):-len(tail)])
    return None


def _known_key(target: _Target) -> str:
    """Clé `known` de la cible (format historique de chaque plateforme)."""
    platform, kind, ident = target
    if kind == "group":
        return f"group:{ident}"
    # sensor.py : `link:{id}` (clé partagée par tous les capteurs du lien) ;
    # binary_sensor.py : l'identifiant du lien tel quel.
    return f"link:{ident}" if platform == "sensor" else ident


def _has_rows(block: Any) -> bool:
    """True si la réponse est un dict dont `rows` est une liste non vide."""
    rows = block.get("rows") if isinstance(block, dict) else None
    return isinstance(rows, list) and bool(rows)


def _gateways_reliable(hub: OPNsenseDataCoordinator, data: dict) -> bool:
    """Données de passerelles exploitables : pas None, interdites ni en repli.

    `wan_links` retombe sur `_fallback_link` quand `gateways` est vide / None :
    ce cas n'est pas une preuve d'absence.
    """
    forbidden = getattr(hub.client, "forbidden", set())
    return "gateways" not in forbidden and _has_rows(data.get("gateways"))


def _statuses_reliable(hub: OPNsenseDataCoordinator, data: dict) -> bool:
    """Statut des passerelles présent (dpinger), requis pour les liens."""
    block = data.get("gateway_status")
    return ("gateway_status" not in getattr(hub.client, "forbidden", set())
            and isinstance(block, dict)
            and isinstance(block.get("items"), list))


def async_setup_pruning(
    hass: HomeAssistant, entry: ConfigEntry, hub: OPNsenseDataCoordinator
) -> Callable[[], None]:
    """Branche le nettoyage sur le polling rapide ; renvoie la désinscription."""
    # Cible -> (cycles réussis consécutifs d'absence, instant de première absence)
    absent: dict[_Target, tuple[int, float]] = {}
    # Un lien sans case « Upstream » n'est retenu que via son groupe
    # (wans.py:_group_members) : une fois l'API des groupes vue, son absence
    # (403, panne) rend la liste des liens non fiable. Les OPNsense sans cette
    # API ne sont pas bloqués à vie. Une entité de groupe déjà au registre
    # prouve que l'API a répondu par le passé (le drapeau survit au reload).
    groups_api_seen = any(
        reg.translation_key == "gateway_group"
        for reg in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
    )

    @callback
    def _prune() -> None:
        nonlocal groups_api_seen
        if not hub.fast.last_update_success:
            absent.clear()  # cycle en échec : on repart de zéro
            return
        data = hub.fast.data or {}
        groups_block = data.get("gateway_groups")
        # Un 403 prouve que l'API existe : jamais le cas « OPNsense ancien »
        groups_forbidden = "gateway_groups" in getattr(
            hub.client, "forbidden", set()
        )
        groups_api_ok = isinstance(groups_block, dict) and not groups_forbidden
        if groups_api_ok:
            groups_api_seen = True
        reliable = _gateways_reliable(hub, data)
        links_ok = (
            reliable
            and _statuses_reliable(hub, data)
            and not groups_forbidden
            and (groups_api_ok or not groups_api_seen)
        )
        groups_ok = reliable and data.get("_wan_groups") is not None
        if not (links_ok or groups_ok):
            absent.clear()
            return

        expected_links = {link["id"] for link in links_with_entities(data)}
        expected_groups = {
            g["name"] for g in data.get("_wan_groups") or [] if g.get("name")
        }

        registry = er.async_get(hass)
        targets: dict[_Target, list[str]] = {}
        for reg in er.async_entries_for_config_entry(registry, entry.entry_id):
            target = _classify(entry.entry_id, reg)
            if target is not None:
                targets.setdefault(target, []).append(reg.entity_id)

        now = monotonic()
        for target, entity_ids in targets.items():
            _platform, kind, ident = target
            ok = links_ok if kind == "link" else groups_ok
            present = ident in (expected_links if kind == "link" else expected_groups)
            if not ok or present:
                absent.pop(target, None)
                continue
            cycles, since = absent.get(target, (0, now))
            cycles += 1
            absent[target] = (cycles, since)
            if cycles < PRUNE_CYCLES or now - since < PRUNE_MIN_SECONDS:
                continue
            for entity_id in entity_ids:
                registry.async_remove(entity_id)
                _LOGGER.info(
                    "Entité %s supprimée : %s '%s' absent depuis %d cycles",
                    entity_id, "lien WAN" if kind == "link" else "groupe", ident,
                    cycles,
                )
            # Permet à _sync_* de recréer les entités si la cible réapparaît.
            hub.dynamic_known.get(target[0], set()).discard(_known_key(target))
            absent.pop(target, None)
        # Cibles disparues du registre : plus de compteur à garder.
        for target in [t for t in absent if t not in targets]:
            absent.pop(target)

    return hub.fast.async_add_listener(_prune)
