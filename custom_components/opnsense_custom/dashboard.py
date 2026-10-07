"""Création automatique du dashboard OPNsense (design "Nocturne") en sidebar.

Conçu pour être "plug and play" : à l'installation, l'intégration pose un
dashboard soigné (bandeau d'état, tuiles à sparkline, trafic 24 h, top
destinations en barres) dans le menu de gauche de Home Assistant.

Points clés :
  * Cartes construites à partir des VRAIS entity_id lus dans le registre
    (via le suffixe d'unique_id) -> indépendant de la langue de l'UI.
  * Seules des entités activées par défaut sont utilisées (les capteurs
    disque total/utilisé/disponible, désactivés d'office, sont évités).
  * Design premium via cartes HACS : Mushroom, apexcharts-card,
    mini-graph-card et card-mod. Si elles manquent, un avertissement est
    loggé (cf. README -> prérequis frontend).
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
)

# Nombre de lignes affichées par liste "top destinations" (attribut top_5).
TOP_ROWS = 5

# Styles card-mod réutilisés.
_GLASS = (
    "ha-card { border-radius: 20px; background: rgba(15,23,42,0.55); "
    "border: 1px solid rgba(148,163,184,0.12); "
    "box-shadow: 0 8px 24px rgba(0,0,0,0.35); "
    "backdrop-filter: blur(14px); -webkit-backdrop-filter: blur(14px); }"
)
_HEADING = (
    "ha-card { background: none; border: none; box-shadow: none; } "
    ".title { text-transform: uppercase; letter-spacing: 1.4px; "
    "font-size: 12px !important; font-weight: 700; opacity: 0.75; }"
)
_KPI = (
    " .state__value { font-weight: 800; } .header .name { opacity: 0.7; "
    "text-transform: uppercase; letter-spacing: 1px; font-size: 11px; }"
)
# Couleurs d'accent (RGB) des barres "top destinations".
_ACCENT_RGB = {"blue": "56,189,248", "purple": "167,139,250"}


def _entity_map(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, str]:
    """Mappe le suffixe d'unique_id -> entity_id réel pour cette entry."""
    registry = er.async_get(hass)
    prefix = f"{entry.entry_id}_"
    mapping: dict[str, str] = {}
    for ent in er.async_entries_for_config_entry(registry, entry.entry_id):
        if ent.unique_id.startswith(prefix):
            mapping[ent.unique_id[len(prefix):]] = ent.entity_id
    return mapping


def _heading(title: str, icon: str) -> dict:
    """Titre de section discret (majuscules espacées)."""
    return {"type": "heading", "heading": title, "icon": icon,
            "card_mod": {"style": _HEADING}}


def _kpi(entity: str, name: str, icon: str, color: str, columns: int,
         decimals: int) -> dict:
    """Tuile chiffre clé + sparkline 6 h (mini-graph-card)."""
    return {
        "type": "custom:mini-graph-card",
        "entities": [{"entity": entity, "color": color}],
        "name": name, "icon": icon,
        "hours_to_show": 6, "points_per_hour": 12, "line_width": 3,
        "height": 70, "decimals": decimals, "animate": True,
        "show": {"fill": "fade", "labels": False, "extrema": False,
                 "legend": False, "points": False},
        "grid_options": {"columns": columns},
        "card_mod": {"style": _GLASS + _KPI},
    }


def _top_list(entity: str, accent: str, icon: str, title: str) -> dict:
    """Classement top destinations : une ligne par rang, barre au prorata.

    La largeur de la barre est relative au débit du n°1 (card-mod accepte
    les templates Jinja dans le style).
    """
    rows: list[dict[str, Any]] = [{
        "type": "heading", "heading": title, "heading_style": "subtitle",
        "icon": icon,
    }]
    top = "(state_attr('" + entity + "','top_5') or [])"
    for i in range(TOP_ROWS):
        pick = ("{% set l = " + top + " %}{% set d = l[" + str(i)
                + "] if l | count > " + str(i) + " else none %}")
        peak = ("{% set mx = (l | map(attribute='rate_bps') | max) "
                "if l | count > 0 else 1 %}")
        pct = "{{ ((d.rate_bps / mx * 100) if d and mx else 0) | round(1) }}%"
        rows.append({
            "type": "custom:mushroom-template-card",
            "primary": pick + "{{ d.name if d else '' }}",
            "secondary": pick + (
                "{% if d %}{% set r = d.rate_bps %}"
                "{{ (r / 1000000) | round(1) ~ ' Mbit/s' if r >= 1000000 "
                "else (r / 1000) | round(0) | int ~ ' kbit/s' }}"
                "{% if d.name != d.address %}  ·  {{ d.address }}{% endif %}"
                "{% endif %}"
            ),
            "icon": f"mdi:numeric-{i + 1}-circle",
            "icon_color": accent,
            "card_mod": {"style": pick + peak + (
                "ha-card { border-radius: 14px; box-shadow: none; "
                "border: 1px solid rgba(148,163,184,0.10); "
                "background: linear-gradient(90deg, rgba("
                + _ACCENT_RGB[accent] + ",0.28) " + pct
                + ", rgba(15,23,42,0.45) " + pct + "); } "
                ".primary { font-size: 13px !important; white-space: nowrap; "
                "overflow: hidden; text-overflow: ellipsis; } "
                ".secondary { font-variant-numeric: tabular-nums; }"
            )},
        })
    return {"type": "vertical-stack", "cards": rows,
            "grid_options": {"columns": 12}}


def _build_dashboard_config(hass: HomeAssistant, entry: ConfigEntry) -> dict:
    """Construit la config lovelace (design Nocturne) depuis les entités réelles."""
    e = _entity_map(hass, entry)

    def s(key: str) -> str:
        return e.get(key, f"sensor.unknown_{key}")

    wan = s("wan_connected")
    upd = s("update_available")

    def wan_on(on: str, off: str) -> str:
        return "{{ '" + on + "' if is_state('" + wan + "','on') else '" + off + "' }}"

    # Uptime OPNsense : "2 days, 02:41:10" / "1 day, 03:00:00" / "02:41:10".
    uptime = (
        "{% set p = states('" + s("uptime") + "').split(', ') %}"
        "{% set hms = p[-1].split(':') %}{% if hms | count == 3 %}"
        "{{ (p[0].split(' ')[0] ~ ' j ') if p | count > 1 else '' }}"
        "{{ hms[0] | int }} h {{ hms[1] }}{% else %}-{% endif %}"
    )

    # ---- Bandeau d'état : vire au rouge si le WAN tombe ----
    hero = {
        "type": "custom:mushroom-template-card",
        "primary": "{{ states('" + s("hostname") + "').split('.')[0] | upper }}",
        "secondary": (
            wan_on("En ligne", "WAN coupé")
            + "  ·  OPNsense {{ states('" + s("opnsense_version")
            + "').split('-')[0] }}  ·  {{ states('" + s("public_ipv4")
            + "') }}  ·  depuis " + uptime
        ),
        "icon": "mdi:shield-lock",
        "icon_color": wan_on("teal", "red"),
        "tap_action": {"action": "more-info", "entity": wan},
        "grid_options": {"columns": "full"},
        "card_mod": {"style": (
            "ha-card { border-radius: 24px; padding: 10px 6px; "
            "border: 1px solid "
            + wan_on("rgba(45,212,191,0.35)", "rgba(248,113,113,0.5)") + "; "
            "background: radial-gradient(120% 140% at 0% 0%, "
            + wan_on("rgba(20,184,166,0.30)", "rgba(239,68,68,0.30)")
            + " 0%, rgba(15,23,42,0.80) 55%); box-shadow: 0 0 32px "
            + wan_on("rgba(45,212,191,0.18)", "rgba(239,68,68,0.25)")
            + ", 0 10px 30px rgba(0,0,0,0.45); backdrop-filter: blur(16px); } "
            ".primary { font-size: 20px !important; font-weight: 800 !important; "
            "letter-spacing: 1.5px; } .secondary { font-variant-numeric: "
            "tabular-nums; opacity: 0.85; font-size: 13px !important; } "
            "ha-state-icon { --mdc-icon-size: 32px; }"
        )},
    }

    # ---- Trafic : tuiles temps réel + courbe 24 h ----
    def serie(entity: str, name: str, color: str) -> dict:
        return {"entity": entity, "name": name, "type": "area",
                "color": color, "stroke_width": 2, "opacity": 0.25,
                "group_by": {"func": "avg", "duration": "10min"}}

    traffic_chart = {
        "type": "custom:apexcharts-card", "graph_span": "24h",
        "header": {"show": True, "title": "Trafic WAN · 24 h"},
        "series": [serie(s("wan_throughput_in"), "Entrant", "#38bdf8"),
                   serie(s("wan_throughput_out"), "Sortant", "#a78bfa")],
        "apex_config": {
            "chart": {"height": 230},
            "legend": {"show": True, "position": "top",
                       "horizontalAlign": "right"},
            "grid": {"borderColor": "rgba(148,163,184,0.10)",
                     "strokeDashArray": 4},
            "yaxis": {"decimalsInFloat": 1, "title": {"text": "Mbit/s"}},
            "tooltip": {"theme": "dark"}, "stroke": {"curve": "smooth"},
            "dataLabels": {"enabled": False},
        },
        "grid_options": {"columns": "full"},
        "card_mod": {"style": _GLASS},
    }
    traffic = {"type": "grid", "column_span": 2, "cards": [
        _heading("Trafic", "mdi:swap-vertical"),
        _kpi(s("wan_throughput_in"), "Entrant", "mdi:arrow-down-bold",
             "#38bdf8", 12, 2),
        _kpi(s("wan_throughput_out"), "Sortant", "mdi:arrow-up-bold",
             "#a78bfa", 12, 2),
        traffic_chart,
    ]}

    # ---- Système : CPU / RAM, disque en barre, firmware ----
    disk_pct = "states('" + s("disk_root_percent") + "') | float(0)"
    disk = {
        "type": "custom:mushroom-template-card", "primary": "Disque /",
        "secondary": "{{ " + disk_pct + " | round(0) | int }} % utilisé",
        "icon": "mdi:harddisk",
        "icon_color": ("{{ 'red' if " + disk_pct + " > 90 else ('amber' if "
                       + disk_pct + " > 75 else 'teal') }}"),
        "tap_action": {"action": "more-info", "entity": s("disk_root_percent")},
        "grid_options": {"columns": 12},
        "card_mod": {"style": (
            "{% set p = " + disk_pct + " %}ha-card { border-radius: 20px; "
            "border: 1px solid rgba(148,163,184,0.12); "
            "background: linear-gradient(90deg, rgba(45,212,191,0.30) {{ p }}%, "
            "rgba(15,23,42,0.55) {{ p }}%); "
            "box-shadow: 0 8px 24px rgba(0,0,0,0.35); }"
        )},
    }
    firmware = {
        "type": "custom:mushroom-template-card", "primary": "Firmware",
        "secondary": (
            "{{ 'Mise à jour ' ~ states('" + s("firmware_latest")
            + "') ~ ' disponible' if is_state('" + upd + "','on') "
            "else 'À jour · ' ~ states('" + s("firmware_installed") + "') }}"
        ),
        "icon": ("{{ 'mdi:package-up' if is_state('" + upd + "','on') "
                 "else 'mdi:package-variant-closed-check' }}"),
        "icon_color": "{{ 'amber' if is_state('" + upd + "','on') else 'green' }}",
        "tap_action": {"action": "more-info", "entity": s("firmware_update")},
        "hold_action": {"action": "perform-action",
                        "perform_action": "button.press",
                        "target": {"entity_id": s("check_updates")}},
        "grid_options": {"columns": 12},
        "card_mod": {"style": _GLASS},
    }
    system = {"type": "grid", "cards": [
        _heading("Système", "mdi:chip"),
        _kpi(s("loadavg_1"), "Charge CPU", "mdi:cpu-64-bit", "#2dd4bf", 6, 2),
        _kpi(s("ram_used_percent"), "RAM", "mdi:memory", "#818cf8", 6, 0),
        disk, firmware,
    ]}

    # ---- Top destinations ----
    top = {"type": "grid", "column_span": 2, "cards": [
        _heading("Top destinations", "mdi:trophy-outline"),
        _top_list(s("wan_top_dest_in"), "blue", "mdi:arrow-down-bold",
                  "Entrant"),
        _top_list(s("wan_top_dest_out"), "purple", "mdi:arrow-up-bold",
                  "Sortant"),
    ]}

    # ---- WAN : compteurs + adresses publiques ----
    def total(entity: str, name: str, icon: str, color: str) -> dict:
        return {
            "type": "custom:mushroom-template-card",
            "primary": "{{ states('" + entity + "') | float(0) | round(1) }} Go",
            "secondary": name, "icon": icon, "icon_color": color,
            "tap_action": {"action": "more-info", "entity": entity},
            "grid_options": {"columns": 6},
            "card_mod": {"style": _GLASS + " .primary { font-variant-numeric: "
                         "tabular-nums; font-weight: 800 !important; }"},
        }

    wan_section = {"type": "grid", "cards": [
        _heading("WAN", "mdi:wan"),
        total(s("wan_total_received"), "Reçu", "mdi:cloud-download", "blue"),
        total(s("wan_total_transmitted"), "Transmis", "mdi:cloud-upload",
              "purple"),
        {"type": "custom:mushroom-template-card",
         "primary": "{{ states('" + s("public_ipv4") + "') }}",
         "secondary": "{{ states('" + s("public_ipv6") + "') }}",
         "icon": "mdi:ip-network", "icon_color": "cyan",
         "multiline_secondary": True, "grid_options": {"columns": 12},
         "card_mod": {"style": _GLASS + " .primary, .secondary { "
                      "font-family: ui-monospace, monospace; }"}},
    ]}

    return {
        "title": "OPNsense",
        "template_version": DASHBOARD_TEMPLATE_VERSION,
        "views": [{
            "title": "Pare-feu", "path": "pare-feu", "type": "sections",
            "max_columns": 3,
            "sections": [
                {"type": "grid", "column_span": 3, "cards": [hero]},
                traffic, system, top, wan_section,
            ],
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
