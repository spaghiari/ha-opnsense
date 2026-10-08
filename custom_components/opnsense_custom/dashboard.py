"""Création automatique du dashboard OPNsense (design "Console") en sidebar.

Conçu pour être "plug and play" : à l'installation, l'intégration pose un
dashboard façon pupitre de supervision (bandeau d'état, tuiles à sparkline,
trafic 24 h, connexion, tunnels VPN, top destinations) dans le menu de gauche
de Home Assistant.

Direction visuelle "Console" : fond graphite, panneaux plats à angles nets,
bande latérale colorée selon l'état, chiffres en monospace et accent orange
OPNsense. Entrant = orange, sortant = bleu glacier (paire complémentaire,
lisible par les daltoniens) ; un état n'est jamais porté par la seule couleur
(toujours doublé d'un texte ou d'une icône).

Points clés :
  * Cartes construites à partir des VRAIS entity_id lus dans le registre
    (via le suffixe d'unique_id) -> indépendant de la langue de l'UI.
  * Seules des entités activées par défaut sont utilisées (les capteurs
    disque total/utilisé/disponible, désactivés d'office, sont évités).
  * Cartes HACS : Mushroom, apexcharts-card, mini-graph-card et card-mod.
    Si elles manquent, un avertissement est loggé (cf. README).
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
# Emplacements de tunnels VPN affichés (les vides sont masqués).
TUNNEL_SLOTS = 8

# ---- Tokens de la direction "Console" ----
_PANEL = "rgba(16,18,21,0.74)"  # surface des cartes, laisse deviner le fond
_LINE = "rgba(255,255,255,0.07)"
_TEXT = "#ECE7E1"         # blanc chaud
_MUTED = "#8C8A86"
_ORANGE = "#F26722"       # orange OPNsense - accent + trafic entrant
_ICE = "#8FD3F4"          # bleu glacier - trafic sortant
_OK = "#3DD68C"
_WARN = "#F5B83D"
_BAD = "#FF5A4E"
_MONO = ("ui-monospace, 'JetBrains Mono', 'Cascadia Mono', 'SF Mono', "
         "Consolas, monospace")

# Image de fond : baie de serveurs aux câbles orange (rappel de l'accent
# OPNsense), assombrie et floutée côté CDN pour garder le texte lisible.
_BACKGROUND = {
    "image": (
        "https://images.unsplash.com/photo-1558494949-ef010cbdcc31"
        "?auto=format&fit=crop&w=2400&q=80&blend=0A0B0D&blend-alpha=55&blur=6"
    ),
    "size": "cover", "alignment": "center", "repeat": "no-repeat",
    "attachment": "fixed",
}

# Panneau : angles nets, filet 1 px, verre fumé sur l'image de fond.
_PANEL_CSS = (
    "ha-card { background: " + _PANEL + "; border: 1px solid " + _LINE + "; "
    "border-radius: 10px; box-shadow: 0 8px 24px rgba(0,0,0,0.35); "
    "backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px); "
    "--primary-text-color: " + _TEXT + "; "
    "--secondary-text-color: " + _MUTED + "; } "
)
_HEADING_CSS = (
    "ha-card { background: none; border: none; box-shadow: none; } "
    ".title { font-family: " + _MONO + "; text-transform: uppercase; "
    "letter-spacing: 2px; font-size: 11px !important; font-weight: 600; "
    "color: " + _MUTED + " !important; } "
    "ha-icon, ha-state-icon { color: " + _ORANGE + " !important; "
    "--mdc-icon-size: 16px; }"
)
_KPI_CSS = (
    ".state__value { font-family: " + _MONO + "; font-weight: 700; "
    "letter-spacing: -0.5px; } .state__uom { font-family: " + _MONO + "; "
    "opacity: 0.6; } .header .name { font-family: " + _MONO + "; "
    "text-transform: uppercase; letter-spacing: 1.5px; font-size: 10.5px; "
    "color: " + _MUTED + "; } .header .icon { color: " + _MUTED + "; }"
)
_MONO_TEXT = (
    ".primary, .secondary { font-family: " + _MONO + "; "
    "font-variant-numeric: tabular-nums; } "
)


def _stripe(color: str) -> str:
    """Bande latérale d'état (couleur littérale ou expression Jinja)."""
    return "ha-card { box-shadow: inset 3px 0 0 " + color + "; } "


def _icon(color: str) -> str:
    """Couleur d'icône Mushroom en hex (icon_color n'accepte que des noms)."""
    return (
        "mushroom-shape-icon { --icon-color: " + color + " !important; "
        "--shape-color: color-mix(in srgb, " + color + " 14%, transparent) "
        "!important; } "
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


def _heading(title: str, icon: str) -> dict:
    """Titre de section : petites capitales monospace, icône orange."""
    return {"type": "heading", "heading": title, "icon": icon,
            "card_mod": {"style": _HEADING_CSS}}


def _kpi(entity: str, name: str, icon: str, color: str, columns: int,
         decimals: int, live: bool = False) -> dict:
    """Tuile chiffre clé + sparkline (mini-graph-card).

    `live` : grande tuile de la bande "Temps réel" (chiffre XL, courbe sur la
    dernière heure, assez de points pour suivre un rafraîchissement de 2 s).
    """
    style = _PANEL_CSS + _KPI_CSS
    if live:
        style += (
            " .state__value { font-size: 30px !important; } "
            "ha-card { border-top: 2px solid " + color + "; }"
        )
    return {
        "type": "custom:mini-graph-card",
        "entities": [{"entity": entity, "color": color}],
        "name": name, "icon": icon,
        "hours_to_show": 1 if live else 6,
        "points_per_hour": 120 if live else 12,
        "line_width": 2, "height": 64 if live else 56, "decimals": decimals,
        "animate": True,
        "show": {"fill": "fade", "labels": False, "extrema": False,
                 "legend": False, "points": False},
        "grid_options": {"columns": columns},
        "card_mod": {"style": style},
    }


def _template_card(primary: str, secondary: str, icon: str, color: str,
                   **extra: Any) -> dict:
    """Carte Mushroom au style Console (bande + icône colorées)."""
    style = extra.pop("style", "")
    card = {
        "type": "custom:mushroom-template-card",
        "primary": primary, "secondary": secondary, "icon": icon,
        "card_mod": {"style": _PANEL_CSS + _stripe(color) + _icon(color)
                     + _MONO_TEXT + style},
    }
    card.update(extra)
    return card


def _top_list(entity: str, color: str, icon: str, title: str) -> dict:
    """Classement top destinations : une ligne par rang, barre au prorata.

    La largeur de la barre est relative au débit du n°1 (card-mod accepte
    les templates Jinja dans le style).
    """
    rows: list[dict[str, Any]] = [{
        "type": "heading", "heading": title, "heading_style": "subtitle",
        "icon": icon, "card_mod": {"style": _HEADING_CSS},
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
            "card_mod": {"style": pick + peak + _MONO_TEXT + _icon(color) + (
                "ha-card { border-radius: 8px; box-shadow: none; "
                "border: 1px solid " + _LINE + "; "
                "--primary-text-color: " + _TEXT + "; "
                "--secondary-text-color: " + _MUTED + "; "
                "background: linear-gradient(90deg, color-mix(in srgb, "
                + color + " 22%, " + _PANEL + ") " + pct + ", " + _PANEL
                + " " + pct + "); } "
                ".primary { font-size: 12.5px !important; white-space: nowrap; "
                "overflow: hidden; text-overflow: ellipsis; }"
            )},
        })
    return {"type": "vertical-stack", "cards": rows,
            "grid_options": {"columns": 12}}


def _tunnel_column(entity: str, slots: range) -> dict:
    """Colonne de tunnels VPN : une ligne par tunnel, vides masquées."""
    cards = []
    for i in slots:
        pick = ("{% set t = state_attr('" + entity + "','tunnels') or [] %}"
                "{% set x = t[" + str(i) + "] if t | count > " + str(i)
                + " else none %}")
        state_color = ("{{ '" + _OK + "' if x and x.up else '" + _BAD + "' }}")
        cards.append({
            "type": "custom:mushroom-template-card",
            "primary": pick + "{{ x.name if x else '' }}",
            "secondary": pick + (
                "{% if x %}{{ 'En ligne' if x.up else 'Coupé' }} · {{ x.kind }}"
                " · {{ x.address or '-' }}"
                "{% if x.remote %} → {{ x.remote }}{% endif %}{% endif %}"
            ),
            "icon": pick + "{{ 'mdi:lock' if x and x.up else 'mdi:lock-off' }}",
            "card_mod": {"style": pick + _PANEL_CSS + _MONO_TEXT
                         + _stripe(state_color) + _icon(state_color)
                         + "{% if not x %}:host { display: none; }{% endif %}"},
        })
    return {"type": "vertical-stack", "cards": cards,
            "grid_options": {"columns": 12}}


def _build_dashboard_config(hass: HomeAssistant, entry: ConfigEntry) -> dict:
    """Construit la config lovelace (design Console) depuis les entités réelles."""
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
    # Latence WAN si mesurée (privilège optionnel "Status: Gateways").
    latency = (
        "{% set ms = states('" + s("wan_latency") + "') %}"
        "{{ '  ·  ' ~ (ms | float | round(1)) ~ ' ms' if is_number(ms) else '' }}"
    )

    # ---- Bandeau d'état : bande + voyant verts, rouges si le WAN tombe ----
    hero = {
        "type": "custom:mushroom-template-card",
        "primary": (
            "{{ states('" + s("hostname") + "').split('.')[0] | upper }}"
        ),
        "secondary": (
            wan_on("● EN LIGNE", "● WAN COUPÉ") + latency
            + "{{ '  ·  ◉ TEMPS RÉEL' if is_number(states('" + s("cpu_usage")
            + "')) else '' }}"
            + "  ·  OPNsense {{ states('" + s("opnsense_version")
            + "').split('-')[0] }}  ·  {{ states('" + s("public_ipv4")
            + "') }}  ·  up " + uptime
        ),
        "icon": "mdi:shield-lock",
        "tap_action": {"action": "more-info", "entity": wan},
        "grid_options": {"columns": "full"},
        "card_mod": {"style": (
            _PANEL_CSS + _MONO_TEXT + _icon(wan_on(_OK, _BAD))
            + "ha-card { --card-primary-font-size: 22px; "
            "--card-primary-line-height: 28px; --card-primary-font-weight: 700; "
            "--card-primary-letter-spacing: 3px; "
            "--card-secondary-font-size: 12.5px; "
            "--card-secondary-line-height: 18px; --icon-size: 48px; "
            "box-shadow: inset 4px 0 0 " + wan_on(_OK, _BAD) + "; "
            "background: radial-gradient(140% 220% at 0% 0%, "
            "color-mix(in srgb, " + _ORANGE + " 16%, transparent) 0%, "
            "transparent 55%), " + _PANEL + "; } "
            ".secondary { color: " + wan_on(_OK, _BAD) + " !important; "
            "letter-spacing: 0.5px; }"
        )},
    }

    # ---- Trafic : tuiles temps réel + courbe 24 h ----
    def serie(entity: str, name: str, color: str) -> dict:
        return {"entity": entity, "name": name, "type": "area",
                "color": color, "stroke_width": 2, "opacity": 0.18,
                "group_by": {"func": "avg", "duration": "10min"}}

    traffic_chart = {
        "type": "custom:apexcharts-card", "graph_span": "24h",
        "header": {"show": True, "title": "TRAFIC WAN · 24 H"},
        "series": [serie(s("wan_throughput_in"), "Entrant", _ORANGE),
                   serie(s("wan_throughput_out"), "Sortant", _ICE)],
        "apex_config": {
            "chart": {"height": 220, "fontFamily": _MONO,
                      "foreColor": _MUTED},
            "legend": {"show": True, "position": "top",
                       "horizontalAlign": "right"},
            "grid": {"borderColor": _LINE, "strokeDashArray": 3},
            "yaxis": {"decimalsInFloat": 1, "title": {"text": "Mbit/s"}},
            "tooltip": {"theme": "dark"}, "stroke": {"curve": "smooth"},
            "dataLabels": {"enabled": False},
        },
        "grid_options": {"columns": "full"},
        "card_mod": {"style": _PANEL_CSS + (
            ".header #header__title { font-family: " + _MONO + "; "
            "letter-spacing: 2px; font-size: 11px; color: " + _MUTED + "; }"
        )},
    }
    # ---- Temps réel : 4 grandes tuiles rafraîchies par le flux OPNsense ----
    traffic = {"type": "grid", "column_span": 2, "cards": [
        _heading("Temps réel", "mdi:lightning-bolt"),
        _kpi(s("wan_throughput_in"), "Débit entrant", "mdi:arrow-down",
             _ORANGE, 6, 1, live=True),
        _kpi(s("wan_throughput_out"), "Débit sortant", "mdi:arrow-up",
             _ICE, 6, 1, live=True),
        _kpi(s("wan_latency"), "Latence", "mdi:timer-outline", _OK, 6, 1,
             live=True),
        _kpi(s("cpu_usage"), "CPU", "mdi:cpu-64-bit", _WARN, 6, 0, live=True),
        traffic_chart,
    ]}

    # ---- Connexion : latence / pertes (dpinger) + services ----
    services = s("services_stopped")
    n = "{% set n = states('" + services + "') %}"
    svc_color = (n + "{{ '" + _MUTED + "' if not is_number(n) else ('" + _OK
                 + "' if n | int == 0 else '" + _BAD + "') }}")
    connection = {"type": "grid", "cards": [
        _heading("Connexion", "mdi:pulse"),
        _kpi(s("wan_packet_loss"), "Pertes de paquets", "mdi:chart-bell-curve",
             _ICE, 12, 1),
        _template_card(
            "Services",
            n + "{% if not is_number(n) %}Droits API manquants (Status: "
            "Services){% elif n | int == 0 %}Tous actifs · {{ state_attr('"
            + services + "','running') }} services{% else %}{{ n }} arrêté(s)"
            " : {{ (state_attr('" + services + "','stopped') or [])"
            " | join(', ') }}{% endif %}",
            n + "{{ 'mdi:cog-outline' if is_number(n) and n | int == 0 "
            "else 'mdi:cog-off-outline' }}",
            svc_color,
            multiline_secondary=True,
            tap_action={"action": "more-info", "entity": services},
            grid_options={"columns": 12},
        ),
    ]}

    # ---- Système : CPU / RAM, disque en barre, firmware ----
    disk_pct = "states('" + s("disk_root_percent") + "') | float(0)"
    disk_color = ("{{ '" + _BAD + "' if " + disk_pct + " > 90 else ('" + _WARN
                  + "' if " + disk_pct + " > 75 else '" + _ORANGE + "') }}")
    disk = _template_card(
        "Disque /",
        "{{ " + disk_pct + " | round(0) | int }} % utilisé",
        "mdi:harddisk", disk_color,
        tap_action={"action": "more-info", "entity": s("disk_root_percent")},
        grid_options={"columns": 12},
        style=(
            "{% set p = " + disk_pct + " %}ha-card { background: "
            "linear-gradient(90deg, color-mix(in srgb, " + disk_color
            + " 20%, " + _PANEL + ") {{ p }}%, " + _PANEL + " {{ p }}%); }"
        ),
    )
    upd_on = "is_state('" + upd + "','on')"
    firmware = _template_card(
        "Firmware",
        "{{ 'Mise à jour ' ~ states('" + s("firmware_latest") + "') ~ "
        "' disponible' if " + upd_on + " else 'À jour · ' ~ states('"
        + s("firmware_installed") + "') }}",
        "{{ 'mdi:package-up' if " + upd_on + " else "
        "'mdi:package-variant-closed-check' }}",
        "{{ '" + _WARN + "' if " + upd_on + " else '" + _OK + "' }}",
        tap_action={"action": "more-info", "entity": s("firmware_update")},
        hold_action={"action": "perform-action",
                     "perform_action": "button.press",
                     "target": {"entity_id": s("check_updates")}},
        grid_options={"columns": 12},
    )
    system = {"type": "grid", "cards": [
        _heading("Système", "mdi:chip"),
        _kpi(s("loadavg_1"), "Charge CPU", "mdi:cpu-64-bit", _ORANGE, 6, 2),
        _kpi(s("ram_used_percent"), "RAM", "mdi:memory", _ICE, 6, 0),
        disk, firmware,
    ]}

    # ---- Top destinations ----
    top = {"type": "grid", "column_span": 2, "cards": [
        _heading("Top destinations", "mdi:podium"),
        _top_list(s("wan_top_dest_in"), _ORANGE, "mdi:arrow-down", "Entrant"),
        _top_list(s("wan_top_dest_out"), _ICE, "mdi:arrow-up", "Sortant"),
    ]}

    # ---- Tunnels VPN (déduits des interfaces, sans privilège en plus) ----
    tunnels = s("vpn_tunnels_up")
    vpn = {"type": "grid", "column_span": 2, "cards": [
        {**_heading("Tunnels VPN", "mdi:vpn"),
         "badges": [{"type": "entity", "entity": tunnels, "show_state": True,
                     "show_icon": False}]},
        _tunnel_column(tunnels, range(0, TUNNEL_SLOTS, 2)),
        _tunnel_column(tunnels, range(1, TUNNEL_SLOTS, 2)),
    ]}

    # ---- WAN : compteurs + adresses publiques ----
    def total(entity: str, name: str, icon: str, color: str) -> dict:
        return _template_card(
            "{{ states('" + entity + "') | float(0) | round(1) }} Go",
            name, icon, color,
            tap_action={"action": "more-info", "entity": entity},
            grid_options={"columns": 6},
            style=".primary { font-weight: 700 !important; }",
        )

    wan_section = {"type": "grid", "cards": [
        _heading("WAN", "mdi:wan"),
        total(s("wan_total_received"), "Reçu", "mdi:arrow-down", _ORANGE),
        total(s("wan_total_transmitted"), "Transmis", "mdi:arrow-up", _ICE),
        _template_card(
            "{{ states('" + s("public_ipv4") + "') }}",
            "{{ states('" + s("public_ipv6") + "') }}",
            "mdi:ip-network", _MUTED,
            multiline_secondary=True, grid_options={"columns": 12},
        ),
    ]}

    return {
        "title": "OPNsense",
        "template_version": DASHBOARD_TEMPLATE_VERSION,
        "views": [{
            "title": "Pare-feu", "path": "pare-feu", "type": "sections",
            "max_columns": 3,
            "background": _BACKGROUND,
            "sections": [
                {"type": "grid", "column_span": 3, "cards": [hero]},
                traffic, connection, top, system, vpn, wan_section,
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
