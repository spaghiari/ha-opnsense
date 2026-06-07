"""Création automatique d'un dashboard OPNsense dans la barre latérale.

Conçu pour être "plug and play" : à l'installation, l'intégration pose un
dashboard prêt à l'emploi (jauges RAM/disque/CPU, débit WAN, top destinations)
visible dans le menu de gauche de Home Assistant.

Points clés :
  * Les cartes sont construites à partir des VRAIS entity_id lus dans le
    registre d'entités (via le suffixe d'unique_id), donc indépendants de la
    langue de l'UI (FR/EN/...).
  * Best-effort et défensif : toute erreur est loggée et n'interrompt JAMAIS
    le chargement de l'intégration (l'API lovelace utilisée est semi-privée).
  * Idempotent : on ne réécrase pas un dashboard que l'utilisateur a édité ;
    on ne sème la config par défaut qu'à la première création.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_CREATE_DASHBOARD,
    DASHBOARD_URL_PATH,
    DEFAULT_CREATE_DASHBOARD,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


def _entity_map(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, str]:
    """Mappe le suffixe d'unique_id -> entity_id réel pour cette entry.

    Nos unique_id valent f"{entry_id}_{cle}" ; on retrouve donc l'entity_id
    courant quelle que soit la langue/le slug généré par HA.
    """
    registry = er.async_get(hass)
    prefix = f"{entry.entry_id}_"
    mapping: dict[str, str] = {}
    for ent in er.async_entries_for_config_entry(registry, entry.entry_id):
        if ent.unique_id.startswith(prefix):
            mapping[ent.unique_id[len(prefix):]] = ent.entity_id
    return mapping


def _build_dashboard_config(hass: HomeAssistant, entry: ConfigEntry) -> dict:
    """Construit la config lovelace (vue 'sections') depuis les entités réelles."""
    e = _entity_map(hass, entry)

    def row(key: str, name: str) -> dict | None:
        eid = e.get(key)
        return {"entity": eid, "name": name} if eid else None

    def compact(items: list[dict | None]) -> list[dict]:
        return [i for i in items if i]

    # ----- Section État -----
    status_cards: list[dict] = [
        {"type": "heading", "heading": "État du firewall"}
    ]
    system_rows = compact(
        [
            row("hostname", "Hostname"),
            row("opnsense_version", "Version OPNsense"),
            row("uptime", "Uptime"),
            row("wan_connected", "WAN"),
            row("public_ipv4", "IP publique"),
        ]
    )
    if system_rows:
        status_cards.append(
            {
                "type": "entities",
                "title": "Système",
                "show_header_toggle": False,
                "entities": system_rows,
            }
        )
    if (upd := e.get("firmware_update")) :
        status_cards.append({"type": "update", "entity": upd})
    if (btn := e.get("check_updates")) :
        status_cards.append(
            {
                "type": "button",
                "entity": btn,
                "name": "Vérifier les mises à jour",
                "icon": "mdi:cloud-search-outline",
                "show_state": False,
            }
        )

    # ----- Section Ressources -----
    resource_cards: list[dict] = [
        {"type": "heading", "heading": "Ressources"}
    ]
    if (ram := e.get("ram_used_percent")) :
        resource_cards.append(
            {
                "type": "gauge",
                "entity": ram,
                "name": "RAM",
                "unit": "%",
                "min": 0,
                "max": 100,
                "severity": {"green": 0, "yellow": 70, "red": 90},
            }
        )
    if (disk := e.get("disk_root_percent")) :
        resource_cards.append(
            {
                "type": "gauge",
                "entity": disk,
                "name": "Disque /",
                "unit": "%",
                "min": 0,
                "max": 100,
                "severity": {"green": 0, "yellow": 75, "red": 90},
            }
        )
    if (cpu := e.get("loadavg_1")) :
        resource_cards.append(
            {
                "type": "gauge",
                "entity": cpu,
                "name": "Charge CPU (1 min)",
                "min": 0,
                "max": 8,
                "needle": True,
                "severity": {"green": 0, "yellow": 4, "red": 6},
            }
        )

    # ----- Section Trafic WAN -----
    wan_cards: list[dict] = [{"type": "heading", "heading": "Trafic WAN"}]
    throughput = compact(
        [
            row("wan_throughput_in", "Entrant"),
            row("wan_throughput_out", "Sortant"),
        ]
    )
    if throughput:
        wan_cards.append(
            {
                "type": "history-graph",
                "title": "Débit WAN",
                "hours_to_show": 24,
                "entities": throughput,
            }
        )
    totals = compact(
        [
            row("wan_total_received", "Reçu"),
            row("wan_total_transmitted", "Transmis"),
        ]
    )
    if totals:
        wan_cards.append(
            {"type": "glance", "title": "Total transféré", "entities": totals}
        )
    top = compact(
        [
            row("wan_top_dest_in", "Download #1"),
            row("wan_top_dest_out", "Upload #1"),
        ]
    )
    if top:
        wan_cards.append(
            {
                "type": "entities",
                "title": "Top destinations",
                "show_header_toggle": False,
                "entities": top,
            }
        )

    sections = [
        {"type": "grid", "cards": cards}
        for cards in (status_cards, resource_cards, wan_cards)
        if len(cards) > 1  # plus que le seul heading
    ]

    return {
        "title": "OPNsense",
        "views": [
            {
                "title": "Vue d'ensemble",
                "path": "vue-ensemble",
                "type": "sections",
                "max_columns": 3,
                "sections": sections,
            }
        ],
    }


def _resolve_url_path(lovelace_data: Any, entry: ConfigEntry) -> str:
    """url_path propre ('opnsense'), suffixé si déjà pris par un autre dashboard."""
    base = DASHBOARD_URL_PATH
    existing = getattr(lovelace_data, "dashboards", {}) or {}
    if base not in existing:
        return base
    # Déjà utilisé : suffixe déterministe par entry (cas multi-firewalls)
    return f"{base}-{entry.entry_id[:8]}"


async def async_register_dashboard(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Pose (ou ré-enregistre) le dashboard OPNsense dans la sidebar.

    Best-effort : toute exception est avalée (juste loggée).
    """
    if not entry.options.get(CONF_CREATE_DASHBOARD, DEFAULT_CREATE_DASHBOARD):
        return

    try:
        from homeassistant.components.lovelace import (  # noqa: PLC0415
            LOVELACE_DATA,
            _register_panel,
        )
        from homeassistant.components.lovelace import (
            dashboard as lovelace_dashboard,
        )
        from homeassistant.components.lovelace.const import (  # noqa: PLC0415
            MODE_STORAGE,
        )

        lovelace_data = hass.data.get(LOVELACE_DATA)
        if lovelace_data is None:
            _LOGGER.debug("lovelace pas encore prêt - dashboard non créé")
            return

        url_path = _resolve_url_path(lovelace_data, entry)
        item = {
            "id": url_path,
            "url_path": url_path,
            "title": "OPNsense",
            "icon": "mdi:wall",
            "show_in_sidebar": True,
            "require_admin": False,
        }

        # Mémorise le url_path choisi pour le retrait à l'unload
        store = hass.data.setdefault(DOMAIN, {}).setdefault("_dashboards", {})
        store[entry.entry_id] = url_path

        storage = lovelace_data.dashboards.get(url_path)
        if storage is None:
            storage = lovelace_dashboard.LovelaceStorage(hass, item)
            lovelace_data.dashboards[url_path] = storage

        # On ne sème la config par défaut QUE si aucune n'existe encore
        # (préserve les éditions de l'utilisateur entre les redémarrages).
        try:
            await storage.async_load(force=False)
            has_config = True
        except Exception:  # noqa: BLE001 - ConfigNotFound & co.
            has_config = False
        if not has_config:
            await storage.async_save(_build_dashboard_config(hass, entry))

        _register_panel(hass, url_path, MODE_STORAGE, item, update=True)
        _LOGGER.debug("Dashboard OPNsense enregistré sur /%s", url_path)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning(
            "Création du dashboard OPNsense impossible (non bloquant): %s", err
        )


async def async_unregister_dashboard(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Retire le panneau de la sidebar (sans supprimer la config stockée)."""
    url_path = (
        hass.data.get(DOMAIN, {}).get("_dashboards", {}).get(entry.entry_id)
    )
    if not url_path:
        return
    try:
        from homeassistant.components import frontend
        from homeassistant.components.lovelace import LOVELACE_DATA  # noqa: PLC0415

        frontend.async_remove_panel(hass, url_path)
        lovelace_data = hass.data.get(LOVELACE_DATA)
        if lovelace_data is not None:
            lovelace_data.dashboards.pop(url_path, None)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Retrait du dashboard %s: %s", url_path, err)


async def async_delete_dashboard(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Supprime définitivement le dashboard (config stockée incluse).

    Appelé lors de la suppression de l'intégration.
    """
    url_path = (
        hass.data.get(DOMAIN, {}).get("_dashboards", {}).pop(entry.entry_id, None)
    )
    if not url_path:
        return
    try:
        from homeassistant.components import frontend
        from homeassistant.components.lovelace import LOVELACE_DATA  # noqa: PLC0415

        frontend.async_remove_panel(hass, url_path)
        lovelace_data = hass.data.get(LOVELACE_DATA)
        if lovelace_data is not None:
            storage = lovelace_data.dashboards.pop(url_path, None)
            if storage is not None and hasattr(storage, "async_delete"):
                await storage.async_delete()
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Suppression du dashboard %s: %s", url_path, err)
