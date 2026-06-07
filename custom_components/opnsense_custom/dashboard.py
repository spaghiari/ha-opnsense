"""Création automatique du dashboard OPNsense (design "Glass NOC") en sidebar.

Conçu pour être "plug and play" : à l'installation, l'intégration pose un
dashboard soigné (glassmorphism, jauges circulaires, débit WAN, top
destinations) dans le menu de gauche de Home Assistant.

Points clés :
  * Cartes construites à partir des VRAIS entity_id lus dans le registre
    (via le suffixe d'unique_id) -> indépendant de la langue de l'UI.
  * Design premium via cartes HACS : Mushroom, apexcharts-card,
    mini-graph-card, stack-in-card et card-mod. Si elles manquent, un
    avertissement est loggé (cf. README -> prérequis frontend).
  * Best-effort et défensif : toute erreur est loggée et n'interrompt JAMAIS
    le chargement de l'intégration (l'API lovelace utilisée est semi-privée).
  * Géré par l'intégration : le gabarit est re-semé quand
    DASHBOARD_TEMPLATE_VERSION augmente (les éditions manuelles sont alors
    remplacées). Désactivable via l'option "create_dashboard".
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_CREATE_DASHBOARD,
    DASHBOARD_TEMPLATE_VERSION,
    DASHBOARD_URL_PATH,
    DEFAULT_CREATE_DASHBOARD,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# Cartes frontend (HACS) requises par le design par défaut.
REQUIRED_RESOURCES = (
    "lovelace-mushroom",
    "apexcharts-card",
    "mini-graph-card",
    "card-mod",
    "stack-in-card",
)

# Styles card-mod réutilisés (glassmorphism).
_GLASS = (
    "ha-card { border-radius: 22px; background: rgba(255,255,255,0.04); "
    "border: 1px solid rgba(255,255,255,0.07); "
    "box-shadow: 0 6px 20px rgba(0,0,0,0.35); backdrop-filter: blur(12px); }"
)
_HERO = (
    "ha-card { border-radius: 24px; background: linear-gradient(135deg, "
    "rgba(20,184,166,0.16) 0%, rgba(15,23,42,0.72) 55%, "
    "rgba(15,23,42,0.78) 100%); border: 1px solid rgba(255,255,255,0.08); "
    "box-shadow: 0 10px 32px rgba(0,0,0,0.45); backdrop-filter: blur(16px); "
    "padding: 6px 4px; } .secondary { font-variant-numeric: tabular-nums; "
    "opacity: 0.85; } ha-state-icon { --mdc-icon-size: 30px; }"
)
_TITLE = (
    ".title { font-size: 16px; font-weight: 600; } .subtitle { opacity: 0.7; }"
)
_MD = (
    "ha-card { box-shadow: none; border: none; background: none; } "
    "ha-markdown { font-variant-numeric: tabular-nums; font-size: 13px; "
    "line-height: 1.7; }"
)


def _entity_map(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, str]:
    """Mappe le suffixe d'unique_id -> entity_id réel pour cette entry."""
    registry = er.async_get(hass)
    prefix = f"{entry.entry_id}_"
    mapping: dict[str, str] = {}
    for ent in er.async_entries_for_config_entry(registry, entry.entry_id):
        if ent.unique_id.startswith(prefix):
            mapping[ent.unique_id[len(prefix):]] = ent.entity_id
    return mapping


def _gauge(entity: str, name: str, severity: dict, maximum: int,
           unit: str | None = None) -> dict:
    """Jauge native HA (aiguille + sévérité), stylée glass.

    On utilise la carte `gauge` cœur de HA plutôt qu'apexcharts radialBar :
    cette dernière ne se dessine pas de façon fiable dans la vue 'sections'
    (cellule de grille effondrée -> spinner infini).
    """
    card: dict[str, Any] = {
        "type": "gauge", "entity": entity, "name": name,
        "min": 0, "max": maximum, "needle": True, "severity": severity,
        "grid_options": {"columns": 4, "rows": 3},
        "card_mod": {"style": _GLASS},
    }
    if unit:
        card["unit"] = unit
    return card


def _build_dashboard_config(hass: HomeAssistant, entry: ConfigEntry) -> dict:
    """Construit la config lovelace (design Glass NOC) depuis les entités réelles."""
    e = _entity_map(hass, entry)

    def s(key: str) -> str:
        return e.get(key, f"sensor.unknown_{key}")

    wan = s("wan_connected")
    upd = s("update_available")
    topin = s("wan_top_dest_in")
    topout = s("wan_top_dest_out")

    hero_secondary = (
        "OPNsense {{ states('" + s("opnsense_version") + "') }}  ·  "
        "IP {{ states('" + s("public_ipv4") + "') }}  ·  "
        "Uptime {{ states('" + s("uptime") + "') }}"
    )
    wan_color = "{{ 'teal' if is_state('" + wan + "','on') else 'red' }}"
    upd_color = "{{ 'amber' if is_state('" + upd + "','on') else 'green' }}"

    # ---- Section gauche : monitoring temps réel ----
    left = {"type": "grid", "cards": [
        {"type": "custom:mushroom-template-card",
         "primary": "{{ states('" + s("hostname") + "') }}",
         "secondary": hero_secondary,
         "icon": "mdi:shield-lock", "icon_color": wan_color,
         "multiline_secondary": True, "tap_action": {"action": "more-info"},
         "grid_options": {"columns": 12, "rows": "auto"},
         "card_mod": {"style": _HERO}},
        {"type": "custom:mushroom-chips-card", "alignment": "center",
         "grid_options": {"columns": 12, "rows": "auto"},
         "card_mod": {"style": "ha-card { border-radius: 22px; background: "
                      "rgba(255,255,255,0.04); border: 1px solid "
                      "rgba(255,255,255,0.07); box-shadow: 0 6px 20px "
                      "rgba(0,0,0,0.35); backdrop-filter: blur(12px); "
                      "padding: 8px 6px; }"},
         "chips": [
            {"type": "template", "icon": "mdi:wan",
             "content": "{{ 'WAN actif' if is_state('" + wan
                        + "','on') else 'WAN coupe' }}",
             "icon_color": wan_color,
             "tap_action": {"action": "more-info", "entity": wan}},
            {"type": "template", "icon": "mdi:download",
             "content": "{{ states('" + s("wan_throughput_in") + "') }} Mbps",
             "icon_color": "blue"},
            {"type": "template", "icon": "mdi:upload",
             "content": "{{ states('" + s("wan_throughput_out") + "') }} Mbps",
             "icon_color": "purple"},
            {"type": "template", "icon": "mdi:update",
             "content": "{{ 'MAJ dispo' if is_state('" + upd
                        + "','on') else 'A jour' }}",
             "icon_color": upd_color,
             "tap_action": {"action": "more-info",
                            "entity": s("firmware_update")}},
         ]},
        _gauge(s("loadavg_1"), "CPU (1 min)",
               {"green": 0, "yellow": 4, "red": 6}, 8),
        _gauge(s("ram_used_percent"), "RAM",
               {"green": 0, "yellow": 70, "red": 90}, 100, "%"),
        _gauge(s("disk_root_percent"), "Disque /",
               {"green": 0, "yellow": 75, "red": 90}, 100, "%"),
        {"type": "custom:mini-graph-card", "name": "Trafic WAN",
         "entities": [
            {"entity": s("wan_throughput_in"), "name": "Entrant",
             "color": "#38bdf8"},
            {"entity": s("wan_throughput_out"), "name": "Sortant",
             "color": "#a78bfa"}],
         "hours_to_show": 6, "points_per_hour": 30, "line_width": 2,
         "smoothing": True,
         "show": {"fill": "fade", "extrema": True, "labels": True,
                  "icon": False, "name": True, "legend": True},
         "height": 90, "grid_options": {"columns": 12, "rows": "auto"},
         "card_mod": {"style": _GLASS}},
    ]}

    # ---- Section droite : compteurs / top / système ----
    counters = {"type": "custom:stack-in-card",
                "grid_options": {"columns": 12, "rows": "auto"},
                "card_mod": {"style": _GLASS}, "cards": [
        {"type": "custom:mushroom-title-card", "title": "Compteurs WAN",
         "card_mod": {"style": "ha-card { padding-bottom: 0; } "
                      ".title { font-size: 16px; font-weight: 600; }"}},
        {"type": "custom:mushroom-template-card",
         "primary": "{{ states('" + s("wan_total_received")
                    + "') | float(0) | round(1) }} Go",
         "secondary": "Total recu", "icon": "mdi:cloud-download",
         "icon_color": "blue",
         "tap_action": {"action": "more-info",
                        "entity": s("wan_total_received")}},
        {"type": "custom:mushroom-template-card",
         "primary": "{{ states('" + s("wan_total_transmitted")
                    + "') | float(0) | round(1) }} Go",
         "secondary": "Total transmis", "icon": "mdi:cloud-upload",
         "icon_color": "purple",
         "tap_action": {"action": "more-info",
                        "entity": s("wan_total_transmitted")}},
        {"type": "custom:mushroom-template-card",
         "primary": "{{ states('" + s("ram_used")
                    + "') | float(0) | round(1) }} Go",
         "secondary": "RAM utilisee", "icon": "mdi:memory",
         "icon_color": "indigo",
         "tap_action": {"action": "more-info", "entity": s("ram_used")}},
    ]}

    top = {"type": "custom:stack-in-card",
           "grid_options": {"columns": 12, "rows": "auto"},
           "card_mod": {"style": _GLASS}, "cards": [
        {"type": "custom:mushroom-title-card", "title": "Top destinations",
         "subtitle": "Debit temps reel (Mbps)",
         "card_mod": {"style": _TITLE}},
        {"type": "markdown",
         "content": "**Entrant - {{ states('" + topin + "') }}**\n\n"
         "{% for d in state_attr('" + topin + "','top_5') or [] %}\n"
         "`{{ d.rate_mbps | float(0) | round(1) }}` Mbps - {{ d.name }}\n"
         "{% endfor %}",
         "card_mod": {"style": _MD}},
        {"type": "markdown",
         "content": "**Sortant - {{ states('" + topout + "') }}**\n\n"
         "{% for d in state_attr('" + topout + "','top_5') or [] %}\n"
         "`{{ d.rate_mbps | float(0) | round(1) }}` Mbps - {{ d.name }}\n"
         "{% endfor %}",
         "card_mod": {"style": _MD}},
    ]}

    boot_fmt = ("{{ as_timestamp(states('" + s("boottime")
                + "')) | timestamp_custom('%d/%m %H:%M', true) }}")
    system = {"type": "custom:stack-in-card",
              "grid_options": {"columns": 12, "rows": "auto"},
              "card_mod": {"style": _GLASS}, "cards": [
        {"type": "custom:mushroom-template-card", "primary": "Firmware",
         "secondary": "{{ 'Mise a jour disponible' if is_state('" + upd
                      + "','on') else 'Systeme a jour' }}",
         "icon": "{{ 'mdi:package-up' if is_state('" + upd
                 + "','on') else 'mdi:package-variant-closed-check' }}",
         "icon_color": upd_color,
         "tap_action": {"action": "more-info",
                        "entity": s("firmware_update")}},
        {"type": "custom:mushroom-template-card",
         "primary": "Verifier les mises a jour",
         "secondary": "Dernier demarrage : " + boot_fmt,
         "icon": "mdi:refresh", "icon_color": "teal",
         "tap_action": {"action": "call-service", "service": "button.press",
                        "target": {"entity_id": s("check_updates")}}},
    ]}

    return {
        "title": "OPNsense",
        "template_version": DASHBOARD_TEMPLATE_VERSION,
        "views": [{
            "title": "Pare-feu", "path": "pare-feu", "type": "sections",
            "max_columns": 2,
            "sections": [left, {"type": "grid",
                                "cards": [counters, top, system]}],
        }],
    }


def _missing_resources(hass: HomeAssistant) -> list[str]:
    """Liste les cartes HACS requises absentes des ressources lovelace."""
    try:
        from homeassistant.components.lovelace import LOVELACE_DATA

        lovelace_data = hass.data.get(LOVELACE_DATA)
        resources = getattr(lovelace_data, "resources", None)
        if resources is None:
            return []
        urls = " ".join(
            item.get("url", "") for item in resources.async_items()
        )
        return [r for r in REQUIRED_RESOURCES if r not in urls]
    except Exception:  # noqa: BLE001
        return []


async def async_register_dashboard(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Pose (ou rafraîchit) le dashboard OPNsense dans la sidebar. Best-effort."""
    if not entry.options.get(CONF_CREATE_DASHBOARD, DEFAULT_CREATE_DASHBOARD):
        return

    try:
        from homeassistant.components.lovelace import (  # noqa: PLC0415
            LOVELACE_DATA,
            _register_panel,
        )
        from homeassistant.components.lovelace import (  # noqa: PLC0415
            dashboard as lovelace_dashboard,
        )
        from homeassistant.components.lovelace.const import (  # noqa: PLC0415
            MODE_STORAGE,
        )

        lovelace_data = hass.data.get(LOVELACE_DATA)
        if lovelace_data is None:
            _LOGGER.debug("lovelace pas encore prêt - dashboard non créé")
            return

        missing = _missing_resources(hass)
        if missing:
            _LOGGER.warning(
                "Dashboard OPNsense : cartes HACS manquantes %s. "
                "Installe-les via HACS (cf. README) pour un rendu correct.",
                ", ".join(missing),
            )

        url_path = DASHBOARD_URL_PATH
        item = {
            "id": url_path, "url_path": url_path, "title": "OPNsense",
            "icon": "mdi:shield-lock", "show_in_sidebar": True,
            "require_admin": False,
        }

        store = hass.data.setdefault(DOMAIN, {}).setdefault("_dashboards", {})
        store[entry.entry_id] = url_path

        storage = lovelace_data.dashboards.get(url_path)
        if storage is None:
            storage = lovelace_dashboard.LovelaceStorage(hass, item)
            lovelace_data.dashboards[url_path] = storage

        # Re-sème le gabarit si absent OU si la version a augmenté.
        try:
            current = await storage.async_load(force=False)
            stored_v = (current or {}).get("template_version", 0)
        except Exception:  # noqa: BLE001 - ConfigNotFound & co.
            stored_v = -1
        if stored_v < DASHBOARD_TEMPLATE_VERSION:
            await storage.async_save(_build_dashboard_config(hass, entry))
            _LOGGER.debug("Dashboard OPNsense semé/rafraîchi (v%s)",
                          DASHBOARD_TEMPLATE_VERSION)

        _register_panel(hass, url_path, MODE_STORAGE, item, update=True)
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
        from homeassistant.components import frontend  # noqa: PLC0415
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
    """Supprime définitivement le dashboard (config stockée incluse)."""
    url_path = (
        hass.data.get(DOMAIN, {}).get("_dashboards", {}).pop(entry.entry_id, None)
    )
    if not url_path:
        return
    try:
        from homeassistant.components import frontend  # noqa: PLC0415
        from homeassistant.components.lovelace import LOVELACE_DATA  # noqa: PLC0415

        frontend.async_remove_panel(hass, url_path)
        lovelace_data = hass.data.get(LOVELACE_DATA)
        if lovelace_data is not None:
            storage = lovelace_data.dashboards.pop(url_path, None)
            if storage is not None and hasattr(storage, "async_delete"):
                await storage.async_delete()
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Suppression du dashboard %s: %s", url_path, err)
