"""Création automatique du dashboard OPNsense en sidebar, en trois thèmes.

Conçu pour être "plug and play" : à l'installation, l'intégration pose un
dashboard de supervision (bandeau d'état, bande temps réel, courbe 24 h,
connexion, températures, système, top destinations, tunnels VPN) dans le
menu de gauche de Home Assistant.

Trois thèmes au choix (option "dashboard_theme"), mêmes composants :
  * Graphite : sobre et mat, une seule couleur vive (orange OPNsense) ;
  * Aurore   : lueurs dégradées en CSS, verre doux, couleurs chaudes ;
  * Cockpit  : instruments, gros chiffres en monospace, ambre et cyan.
Un état n'est jamais porté par la seule couleur (toujours doublé d'un texte
ou d'une icône) ; entrant / sortant restent une paire complémentaire.

Points clés :
  * Cartes construites à partir des VRAIS entity_id lus dans le registre
    (via le suffixe d'unique_id) -> indépendant de la langue de l'UI.
  * Cartes HACS : Mushroom, apexcharts-card, mini-graph-card et card-mod.
    Si elles manquent, un avertissement est loggé (cf. README).
  * Best-effort et défensif : toute erreur est loggée et n'interrompt JAMAIS
    le chargement de l'intégration (l'API lovelace utilisée est semi-privée).
  * Géré par l'intégration : le gabarit est re-semé quand
    DASHBOARD_TEMPLATE_VERSION augmente ou quand le thème change (les
    éditions manuelles sont alors remplacées). Désactivable via l'option
    "create_dashboard".
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_CREATE_DASHBOARD,
    CONF_DASHBOARD_THEME,
    DASHBOARD_TEMPLATE_VERSION,
    DASHBOARD_URL_PATH,
    DEFAULT_CREATE_DASHBOARD,
    DEFAULT_DASHBOARD_THEME,
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
# Emplacements de tunnels VPN affichés (les vides sont masqués).
TUNNEL_SLOTS = 8

_SYS = "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
_MONO = "ui-monospace, 'Cascadia Mono', 'SF Mono', Consolas, monospace"

# ---- Tokens des trois thèmes (cf. maquette validée) ----
THEMES: dict[str, dict[str, Any]] = {
    "graphite": {
        "background": "#0f1114",
        "card": "#16191d", "line": "#22252b", "track": "#22252b",
        "text": "#ebe9e5", "muted": "#8d8b87",
        "accent": "#f26722", "in": "#f26722", "out": "#9db4c8",
        "ok": "#4fc38a", "warn": "#d8a648", "bad": "#e5604f",
        "radius": 12, "blur": None, "shadow": "none",
        "hero_bg": "#16191d", "hero_line": "#22252b",
        "font_label": _SYS, "font_num": _MONO, "font_display": _SYS,
        "name_track": "0.5px", "kpi_size": 28, "kpi_bar": False,
    },
    "aurore": {
        "background": (
            "radial-gradient(40% 35% at 15% 10%, rgba(242,103,34,0.28), "
            "transparent 70%) fixed, radial-gradient(35% 30% at 85% 20%, "
            "rgba(56,189,248,0.22), transparent 70%) fixed, radial-gradient("
            "45% 40% at 60% 95%, rgba(124,92,255,0.18), transparent 70%) fixed, "
            "#0b1020"
        ),
        "card": "rgba(255,255,255,0.045)", "line": "rgba(255,255,255,0.08)",
        "track": "rgba(255,255,255,0.08)",
        "text": "#eef1fb", "muted": "#97a0bf",
        "accent": "#ff8a4c", "in": "#ff8a4c", "out": "#5cc8ff",
        "ok": "#5ee0a8", "warn": "#ffc857", "bad": "#ff6b7a",
        "radius": 16, "blur": "blur(14px)",
        "shadow": "0 10px 30px rgba(3,6,20,0.45)",
        "hero_bg": (
            "linear-gradient(120deg, rgba(242,103,34,0.20), "
            "rgba(255,255,255,0.04) 45%, rgba(56,189,248,0.14))"
        ),
        "hero_line": "rgba(255,255,255,0.12)",
        "font_label": _SYS, "font_num": _SYS, "font_display": _SYS,
        "name_track": "1px", "kpi_size": 30, "kpi_bar": False,
    },
    "cockpit": {
        "background": "#0d131c",
        "card": "#121a25", "line": "#1d2734", "track": "#1d2734",
        "text": "#e6edf5", "muted": "#7f8ea3",
        "accent": "#ffb020", "in": "#ffb020", "out": "#3ec7e0",
        "ok": "#39d98a", "warn": "#ffb020", "bad": "#ff5c5c",
        "radius": 6, "blur": None, "shadow": "none",
        "hero_bg": "#121a25", "hero_line": "#1d2734",
        "font_label": _MONO, "font_num": _MONO, "font_display": _MONO,
        "name_track": "3px", "kpi_size": 32, "kpi_bar": True,
    },
}


def _theme(entry: ConfigEntry) -> tuple[str, dict[str, Any]]:
    """Thème choisi dans les options (repli sur le thème par défaut)."""
    name = entry.options.get(CONF_DASHBOARD_THEME, DEFAULT_DASHBOARD_THEME)
    if name not in THEMES:
        name = DEFAULT_DASHBOARD_THEME
    return name, THEMES[name]


# ---- Fragments de style (card-mod) ----

def _panel(th: dict) -> str:
    """Surface d'une carte selon le thème."""
    blur = (
        f"backdrop-filter: {th['blur']}; -webkit-backdrop-filter: {th['blur']}; "
        if th["blur"] else ""
    )
    return (
        "ha-card { background: " + th["card"] + "; border: 1px solid "
        + th["line"] + "; border-radius: " + str(th["radius"]) + "px; "
        "box-shadow: " + th["shadow"] + "; " + blur
        + "--primary-text-color: " + th["text"] + "; "
        "--secondary-text-color: " + th["muted"] + "; } "
    )


def _icon(color: str) -> str:
    """Couleur d'icône Mushroom (couleur littérale ou expression Jinja)."""
    return (
        "mushroom-shape-icon { --icon-color: " + color + " !important; "
        "--shape-color: color-mix(in srgb, " + color + " 15%, transparent) "
        "!important; } "
    )


def _nums(th: dict) -> str:
    """Chiffres alignés dans la police des valeurs du thème."""
    return (
        ".primary { font-family: " + th["font_num"] + "; "
        "font-variant-numeric: tabular-nums; } .secondary { font-family: "
        + th["font_label"] + "; } "
    )


def _meter(th: dict, tone: str, pct: str) -> str:
    """Jauge en liseré au ras du bord bas ; `pct` est une expression Jinja.

    Hors du flux : elle ne chevauche pas le texte et ne décale pas le contenu.
    """
    return (
        "{% set p = [[(" + pct + ") | float(0), 0] | max, 100] | min %}"
        "ha-card { position: relative; overflow: hidden; } "
        "ha-card::after { content: ''; position: absolute; left: 0; "
        "right: 0; bottom: 0; height: 4px; pointer-events: none; "
        "background: linear-gradient(90deg, " + tone + " {{ p }}%, "
        + th["track"] + " {{ p }}%); } "
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


# ---- Composants ----

def _heading(th: dict, title: str, icon: str, **extra: Any) -> dict:
    """Titre de section : petites capitales, icône d'accent."""
    card = {
        "type": "heading", "heading": title, "icon": icon,
        "card_mod": {"style": (
            "ha-card { background: none; border: none; box-shadow: none; } "
            ".title { font-family: " + th["font_label"] + "; "
            "text-transform: uppercase; letter-spacing: 1.6px; "
            "font-size: 11px !important; font-weight: 600; color: "
            + th["muted"] + " !important; } ha-icon, ha-state-icon { color: "
            + th["accent"] + " !important; --mdc-icon-size: 16px; }"
        )},
    }
    card.update(extra)
    return card


def _kpi(th: dict, entity: str, name: str, icon: str, tone: str,
         columns: int, decimals: int, live: bool = False) -> dict:
    """Tuile chiffre clé + sparkline (mini-graph-card).

    `live` : grande tuile de la bande "Temps réel" (chiffre XL, courbe sur la
    dernière heure, assez de points pour suivre un rafraîchissement de 2 s).
    """
    style = _panel(th) + (
        ".state__value { font-family: " + th["font_num"] + "; "
        "font-weight: 700; letter-spacing: -0.5px; } .state__uom { "
        "font-family: " + _SYS + "; opacity: 0.6; } .header .name { "
        "font-family: " + th["font_label"] + "; text-transform: uppercase; "
        "letter-spacing: 1.3px; font-size: 10.5px; color: " + th["muted"]
        + "; } .header .icon { color: " + tone + "; } "
    )
    if live:
        size = str(th["kpi_size"])
        style += ".state__value { font-size: " + size + "px !important; } "
        if th["kpi_bar"]:
            style += "ha-card { box-shadow: inset 3px 0 0 " + tone + "; } "
    return {
        "type": "custom:mini-graph-card",
        "entities": [{"entity": entity, "color": tone}],
        "name": name, "icon": icon,
        "hours_to_show": 1 if live else 6,
        "points_per_hour": 120 if live else 12,
        "line_width": 2, "height": 56, "decimals": decimals, "animate": True,
        "show": {"fill": "fade", "labels": False, "extrema": False,
                 "legend": False, "points": False},
        "grid_options": {"columns": columns},
        "card_mod": {"style": style},
    }


def _tile(th: dict, primary: str, secondary: str, icon: str, tone: str,
          style: str = "", **extra: Any) -> dict:
    """Carte Mushroom au style du thème (icône teintée)."""
    card = {
        "type": "custom:mushroom-template-card",
        "primary": primary, "secondary": secondary, "icon": icon,
        "card_mod": {"style": _panel(th) + _icon(tone) + _nums(th) + style},
    }
    card.update(extra)
    return card


def _top_list(th: dict, entity: str, tone: str, icon: str, title: str) -> dict:
    """Classement top destinations : une ligne par rang, barre au prorata."""
    rows: list[dict[str, Any]] = [{
        "type": "heading", "heading": title, "heading_style": "subtitle",
        "icon": icon,
        "card_mod": {"style": "ha-icon { color: " + tone + " !important; }"},
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
            "primary": pick + "{{ d.name if d else '-' }}",
            "secondary": pick + (
                "{% if d %}{% set r = d.rate_bps %}"
                "{{ (r / 1000000) | round(1) ~ ' Mbit/s' if r >= 1000000 "
                "else (r / 1000) | round(0) | int ~ ' kbit/s' }}"
                "{% if d.name != d.address %}  ·  {{ d.address }}{% endif %}"
                "{% endif %}"
            ),
            "icon": f"mdi:numeric-{i + 1}",
            "card_mod": {"style": pick + peak + _panel(th) + _icon(tone) + (
                "ha-card { background: linear-gradient(90deg, color-mix(in "
                "srgb, " + tone + " 18%, transparent) " + pct + ", transparent "
                + pct + "), " + th["card"] + "; } .primary { font-size: 12.5px "
                "!important; white-space: nowrap; overflow: hidden; "
                "text-overflow: ellipsis; } .secondary { font-family: "
                + th["font_num"] + "; }"
            )},
        })
    return {"type": "vertical-stack", "cards": rows,
            "grid_options": {"columns": 12}}


def _tunnels(th: dict, entity: str) -> dict:
    """Une ligne par tunnel VPN, dans une pile verticale.

    La pile est indispensable : dans une section, une carte masquée garde sa
    cellule de grille (trou visible), alors qu'une pile la retire vraiment.
    """
    cards = []
    for i in range(TUNNEL_SLOTS):
        pick = ("{% set t = state_attr('" + entity + "','tunnels') or [] %}"
                "{% set x = t[" + str(i) + "] if t | count > " + str(i)
                + " else none %}")
        tone = "{{ '" + th["ok"] + "' if x and x.up else '" + th["bad"] + "' }}"
        cards.append({
            "type": "custom:mushroom-template-card",
            "primary": pick + "{{ x.name | title if x else '' }}",
            "secondary": pick + (
                "{% if x %}{{ 'En ligne' if x.up else 'Coupé' }} · {{ x.kind }}"
                " · {{ x.address or '-' }}"
                "{% if x.remote %} → {{ x.remote }}{% endif %}{% endif %}"
            ),
            "icon": pick + "{{ 'mdi:lock' if x and x.up else 'mdi:lock-off' }}",
            "card_mod": {"style": pick + _panel(th) + _icon(tone)
                         + ".secondary { font-family: " + th["font_num"] + "; } "
                         + "{% if not x %}:host { display: none; }{% endif %}"},
        })
    return {"type": "vertical-stack", "cards": cards,
            "grid_options": {"columns": 12}}


def _build_dashboard_config(hass: HomeAssistant, entry: ConfigEntry) -> dict:
    """Construit la config lovelace dans le thème choisi."""
    theme_name, th = _theme(entry)
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

    # ---- Bandeau d'état ----
    state_tone = wan_on(th["ok"], th["bad"])
    hero = {
        "type": "custom:mushroom-template-card",
        "primary": "{{ states('" + s("hostname") + "').split('.')[0] | upper }}",
        "secondary": (
            wan_on("● En ligne", "● WAN coupé")
            + "{{ '   ◉ Temps réel' if is_number(states('" + s("cpu_usage")
            + "')) else '' }}   ·   OPNsense {{ states('" + s("opnsense_version")
            + "').split('-')[0] }}   ·   {{ states('" + s("public_ipv4")
            + "') }}   ·   up " + uptime
        ),
        "icon": "mdi:shield-lock",
        "tap_action": {"action": "more-info", "entity": wan},
        "grid_options": {"columns": "full"},
        "card_mod": {"style": (
            _panel(th) + _icon(state_tone)
            + "ha-card { background: " + th["hero_bg"] + "; border-color: "
            + th["hero_line"] + "; --card-primary-font-size: 21px; "
            "--card-primary-line-height: 28px; --card-primary-font-weight: 700; "
            "--card-primary-letter-spacing: " + th["name_track"] + "; "
            "--card-secondary-font-size: 12.5px; "
            "--card-secondary-line-height: 18px; --icon-size: 46px; "
            "padding: 6px 4px; } .primary { font-family: " + th["font_display"]
            + "; } .secondary { font-family: " + _MONO + "; }"
        )},
    }

    # ---- Temps réel ----
    def serie(entity: str, name: str, color: str) -> dict:
        return {"entity": entity, "name": name, "type": "area",
                "color": color, "stroke_width": 2, "opacity": 0.16,
                # Pic par tranche de 5 min : une moyenne écraserait un burst
                # de quelques secondes (speedtest, téléchargement).
                "group_by": {"func": "max", "duration": "5min"}}

    chart = {
        "type": "custom:apexcharts-card", "graph_span": "24h",
        "header": {"show": True, "title": "TRAFIC WAN · 24 H"},
        "series": [serie(s("wan_throughput_in"), "Entrant", th["in"]),
                   serie(s("wan_throughput_out"), "Sortant", th["out"])],
        "apex_config": {
            "chart": {"height": 200, "fontFamily": th["font_label"],
                      "foreColor": th["muted"]},
            "legend": {"show": True, "position": "top",
                       "horizontalAlign": "right"},
            "grid": {"borderColor": th["line"], "strokeDashArray": 3},
            "yaxis": {"decimalsInFloat": 1, "title": {"text": "Mbit/s"}},
            "tooltip": {"theme": "dark"}, "stroke": {"curve": "smooth"},
            "dataLabels": {"enabled": False},
        },
        "grid_options": {"columns": "full"},
        "card_mod": {"style": _panel(th) + (
            ".header #header__title { font-family: " + th["font_label"] + "; "
            "letter-spacing: 1.4px; font-size: 11px; color: " + th["muted"] + "; }"
        )},
    }
    live = {"type": "grid", "column_span": 2, "cards": [
        _heading(th, "Temps réel", "mdi:lightning-bolt"),
        _kpi(th, s("wan_throughput_in"), "↓ Entrant", "mdi:arrow-down",
             th["in"], 6, 1, live=True),
        _kpi(th, s("wan_throughput_out"), "↑ Sortant", "mdi:arrow-up",
             th["out"], 6, 1, live=True),
        _kpi(th, s("wan_latency"), "Latence", "mdi:timer-outline", th["ok"],
             6, 1, live=True),
        _kpi(th, s("cpu_usage"), "CPU", "mdi:cpu-64-bit", th["warn"], 6, 0,
             live=True),
        chart,
    ]}

    # ---- Colonne de droite : connexion, températures, système ----
    loss = "states('" + s("wan_packet_loss") + "')"
    services = s("services_stopped")
    n = "{% set n = states('" + services + "') %}"
    svc_tone = (n + "{{ '" + th["muted"] + "' if not is_number(n) else ('"
                + th["ok"] + "' if n | int == 0 else '" + th["bad"] + "') }}")
    upd_on = "is_state('" + upd + "','on')"

    def temp_tile(key: str, label: str, warn_at: int, bad_at: int) -> dict:
        ent = s(key)
        val = "states('" + ent + "') | float(0)"
        tone = ("{{ '" + th["bad"] + "' if " + val + " >= " + str(bad_at)
                + " else ('" + th["warn"] + "' if " + val + " >= " + str(warn_at)
                + " else '" + th["ok"] + "') }}")
        return _tile(
            th,
            "{{ " + val + " | round(1) }} °C", label, "mdi:thermometer", tone,
            style=_meter(th, tone, val) + ".primary { color: " + tone
            + " !important; font-weight: 700 !important; }",
            tap_action={"action": "more-info", "entity": ent},
            grid_options={"columns": 6},
            visibility=[{"condition": "state", "entity": ent,
                         "state_not": ["unknown", "unavailable"]}],
        )

    side = {"type": "grid", "cards": [
        _heading(th, "Connexion", "mdi:pulse"),
        _tile(
            th, "{{ " + loss + " | float(0) | round(1) }} %", "Pertes de paquets",
            "mdi:chart-bell-curve", th["ok"],
            style=_meter(th, th["ok"], loss + " | float(0) * 10"),
            tap_action={"action": "more-info", "entity": s("wan_packet_loss")},
            grid_options={"columns": 12},
        ),
        _tile(
            th, "Services",
            n + "{% if not is_number(n) %}Droits API manquants (Status: Services)"
            "{% elif n | int == 0 %}{{ state_attr('" + services + "','running') }}"
            " / {{ state_attr('" + services + "','total') }} actifs"
            "{% else %}{{ n }} arrêté(s) : {{ (state_attr('" + services
            + "','stopped') or []) | join(', ') }}{% endif %}",
            n + "{{ 'mdi:cog-outline' if is_number(n) and n | int == 0 "
            "else 'mdi:cog-off-outline' }}",
            svc_tone, multiline_secondary=True,
            tap_action={"action": "more-info", "entity": services},
            grid_options={"columns": 12},
        ),
        _tile(
            th, "Firmware",
            "{{ 'Mise à jour ' ~ states('" + s("firmware_latest") + "') ~ "
            "' disponible' if " + upd_on + " else 'À jour · ' ~ states('"
            + s("firmware_installed") + "') }}",
            "{{ 'mdi:package-up' if " + upd_on + " else "
            "'mdi:package-variant-closed-check' }}",
            "{{ '" + th["warn"] + "' if " + upd_on + " else '" + th["ok"] + "' }}",
            tap_action={"action": "more-info", "entity": s("firmware_update")},
            hold_action={"action": "perform-action",
                         "perform_action": "button.press",
                         "target": {"entity_id": s("check_updates")}},
            grid_options={"columns": 12},
        ),
        _heading(th, "Températures", "mdi:thermometer"),
        temp_tile("temp_cpu", "CPU", 60, 75),
        temp_tile("temp_sfp", "SFP", 50, 65),
        _heading(th, "Système", "mdi:chip"),
        _tile(
            th, "{{ states('" + s("ram_used_percent") + "') | float(0) | round(1) }} %",
            "RAM", "mdi:memory", th["out"],
            style=_meter(th, th["out"], "states('" + s("ram_used_percent") + "')"),
            tap_action={"action": "more-info", "entity": s("ram_used_percent")},
            grid_options={"columns": 6},
        ),
        _tile(
            th, "{{ states('" + s("disk_root_percent")
            + "') | float(0) | round(0) | int }} %",
            "Disque /", "mdi:harddisk", th["in"],
            style=_meter(th, th["in"], "states('" + s("disk_root_percent") + "')"),
            tap_action={"action": "more-info", "entity": s("disk_root_percent")},
            grid_options={"columns": 6},
        ),
    ]}

    # ---- Top destinations ----
    top = {"type": "grid", "column_span": 2, "cards": [
        _heading(th, "Top destinations", "mdi:podium"),
        _top_list(th, s("wan_top_dest_in"), th["in"], "mdi:arrow-down", "Entrant"),
        _top_list(th, s("wan_top_dest_out"), th["out"], "mdi:arrow-up", "Sortant"),
    ]}

    # ---- Tunnels VPN + volumes WAN ----
    tunnels = s("vpn_tunnels_up")

    def total(entity: str, name: str, icon: str, tone: str) -> dict:
        return _tile(
            th, "{{ states('" + entity + "') | float(0) | round(1) }} Go",
            name, icon, tone,
            tap_action={"action": "more-info", "entity": entity},
            grid_options={"columns": 6},
        )

    vpn = {"type": "grid", "cards": [
        _heading(th, "Tunnels VPN", "mdi:vpn",
                 badges=[{"type": "entity", "entity": tunnels,
                          "show_state": True, "show_icon": False}]),
        _tunnels(th, tunnels),
        _heading(th, "Volumes WAN", "mdi:swap-vertical"),
        total(s("wan_total_received"), "Reçu", "mdi:arrow-down", th["in"]),
        total(s("wan_total_transmitted"), "Transmis", "mdi:arrow-up", th["out"]),
    ]}

    return {
        "title": "OPNsense",
        "template_version": DASHBOARD_TEMPLATE_VERSION,
        "theme": theme_name,
        "views": [{
            "title": "Pare-feu", "path": "pare-feu", "type": "sections",
            "max_columns": 3,
            "background": th["background"],
            "sections": [
                {"type": "grid", "column_span": 3, "cards": [hero]},
                live, side, top, vpn,
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


def _dashboard_url(hass: HomeAssistant, entry: ConfigEntry) -> tuple[str, str]:
    """url_path et titre du dashboard de CE firewall.

    Le premier firewall configuré garde "opnsense" (rétro-compatible) ; les
    suivants obtiennent "opnsense-2", "opnsense-3"... pour ne pas s'écraser.
    """
    entry_ids = [e.entry_id for e in hass.config_entries.async_entries(DOMAIN)]
    rank = entry_ids.index(entry.entry_id) + 1 if entry.entry_id in entry_ids else 1
    if rank == 1:
        return DASHBOARD_URL_PATH, "OPNsense"
    return f"{DASHBOARD_URL_PATH}-{rank}", f"OPNsense {rank}"


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

        url_path, title = _dashboard_url(hass, entry)
        item = {
            "id": url_path, "url_path": url_path, "title": title,
            "icon": "mdi:shield-lock", "show_in_sidebar": True,
            "require_admin": False,
        }

        store = hass.data.setdefault(DOMAIN, {}).setdefault("_dashboards", {})
        store[entry.entry_id] = url_path

        storage = lovelace_data.dashboards.get(url_path)
        if storage is None:
            storage = lovelace_dashboard.LovelaceStorage(hass, item)
            lovelace_data.dashboards[url_path] = storage

        try:
            current = await storage.async_load(force=False)
            stored_v = (current or {}).get("template_version", 0)
            stored_theme = (current or {}).get("theme")
        except Exception:  # noqa: BLE001 - ConfigNotFound & co.
            stored_v, stored_theme = -1, None
        # Re-sème si le gabarit a évolué ou si l'utilisateur a changé de thème.
        if stored_v < DASHBOARD_TEMPLATE_VERSION or stored_theme != _theme(entry)[0]:
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
