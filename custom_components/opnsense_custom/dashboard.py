"""Création automatique du dashboard OPNsense en sidebar, en trois thèmes.

Conçu pour être "plug and play" : à l'installation, l'intégration pose un
dashboard de supervision (bandeau d'état, liens WAN en multi-WAN, bande
temps réel, courbe 24 h, connexion, températures, système, top
destinations, tunnels VPN) dans le menu de gauche de Home Assistant.

Trois thèmes au choix (option "dashboard_theme"), mêmes composants :
  * Graphite : sobre et mat, une seule couleur vive (orange OPNsense) ;
  * Aurore   : lueurs dégradées en CSS, verre doux, couleurs chaudes ;
  * Cockpit  : instruments, gros chiffres en monospace, ambre et cyan.
Un état n'est jamais porté par la seule couleur (toujours doublé d'un texte
ou d'une icône) ; entrant / sortant restent une paire complémentaire.

Points clés :
  * Cartes construites à partir des VRAIS entity_id lus dans le registre
    (via le suffixe d'unique_id) -> indépendant de la langue de l'UI.
  * Cartes HACS : button-card, apexcharts-card, mini-graph-card et card-mod.
    Si elles manquent, un avertissement est loggé (cf. README).
  * Best-effort et défensif : toute erreur est loggée et n'interrompt JAMAIS
    le chargement de l'intégration (l'API lovelace utilisée est semi-privée).
  * Géré par l'intégration : le gabarit est re-semé quand
    DASHBOARD_TEMPLATE_VERSION augmente, quand le thème change, quand les
    liens WAN / groupes de passerelles changent ou qu'une entité du firewall
    est renommée. JAMAIS d'écrasement silencieux : l'empreinte de la config
    semée est mémorisée (registre persistant `opnsense_custom.dashboards`,
    qui fige aussi l'URL de chaque firewall) ; si le dashboard a été retouché,
    une alerte Réparations propose de le réinitialiser (au choix de
    l'utilisateur). Limite connue : ce dashboard n'apparaît pas dans
    Paramètres > Tableaux de bord (panneau enregistré hors de la collection
    lovelace). Désactivable via l'option "create_dashboard".
  * Nécessite Home Assistant 2025.2.0+ (LOVELACE_DATA) ; avant, un warning
    unique est loggé et le dashboard est ignoré.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store

from .const import (
    CONF_CREATE_DASHBOARD,
    CONF_DASHBOARD_THEME,
    DASHBOARD_TEMPLATE_VERSION,
    DASHBOARD_URL_PATH,
    DEFAULT_CREATE_DASHBOARD,
    DEFAULT_DASHBOARD_THEME,
    DOMAIN,
)
from .repairs import create_dashboard_issue, delete_dashboard_issue
from .wans import links_with_entities

_LOGGER = logging.getLogger(__name__)

# Cartes frontend (HACS) requises par le design par défaut.
REQUIRED_RESOURCES = (
    "button-card",
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
        "name_track": "0.5px", "kpi_size": 28, "kpi_bar": False, "bar_o": 0.14,
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
        "name_track": "1px", "kpi_size": 30, "kpi_bar": False, "bar_o": 0.18,
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
        "name_track": "3px", "kpi_size": 32, "kpi_bar": True, "bar_o": 0.16,
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
    """Surface d'une carte selon le thème (card-mod)."""
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


def _entity_map(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, str]:
    """Mappe le suffixe d'unique_id -> entity_id réel pour cette entry."""
    registry = er.async_get(hass)
    prefix = f"{entry.entry_id}_"
    mapping: dict[str, str] = {}
    for ent in er.async_entries_for_config_entry(registry, entry.entry_id):
        if ent.unique_id.startswith(prefix):
            mapping[ent.unique_id[len(prefix):]] = ent.entity_id
    return mapping


# ---- Cartes HTML (button-card) ----
# La maquette demande des mises en page (étiquette à gauche / valeur à droite,
# pastilles, jauges arrondies, carré de rang teinté) que les cartes Mushroom
# ne savent pas faire : on rend ces cartes en HTML via button-card. Le code JS
# est évalué côté navigateur à chaque changement d'état des entités suivies.

# Fonctions communes injectées en tête de chaque gabarit JS.
_JS_LIB = (
    "const S=(e)=>{const o=states[e];return o?o.state:'unavailable';};"
    "const N=(e)=>parseFloat(S(e));"
    "const A=(e,a)=>{const o=states[e];"
    "return o&&o.attributes?o.attributes[a]:undefined;};"
    "const soft=(c,p)=>`color-mix(in srgb, ${c} ${p||15}%, transparent)`;"
    "const fr=(v,d)=>isNaN(v)?'–':Number(v).toLocaleString('fr-FR',"
    "{minimumFractionDigits:d,maximumFractionDigits:d});"
    "const esc=(s)=>String(s==null?'':s).replace(/[&<>\"]/g,(c)=>"
    "({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c]));"
    "const pill=(t,c,live)=>`<span style=\"display:inline-flex;align-items:center;"
    "gap:6px;padding:2px 9px;border-radius:999px;font:600 11.5px ${T.sys};"
    "background:${soft(c)};color:${c};white-space:nowrap\"><i class=\"${live?'lv':''}\""
    " style=\"width:7px;height:7px;border-radius:50%;background:currentColor\"></i>"
    "${t}</span>`;"
    "const meter=(p,c)=>`<div style=\"height:6px;border-radius:99px;"
    "background:${T.track};overflow:hidden;margin-top:8px\"><i style=\"display:block;"
    "height:100%;width:${Math.max(0,Math.min(100,p||0))}%;border-radius:inherit;"
    "background:${c}\"></i></div>`;"
    "const lab=(t)=>`<span style=\"font:600 11px ${T.lf};letter-spacing:1.2px;"
    "text-transform:uppercase;color:${T.muted}\">${t}</span>`;"
    "const row2=(title,badge,left,right)=>`<div style=\"padding:14px 16px\">"
    "<div style=\"display:flex;justify-content:space-between;align-items:center;"
    "gap:10px\"><b style=\"font:600 13px ${T.sys}\">${title}</b>${badge}</div>"
    "<div style=\"display:flex;justify-content:space-between;gap:10px;margin-top:6px;"
    "font:500 12px ${T.sys};color:${T.muted}\"><span style=\"white-space:nowrap\">"
    "${left}</span><span style=\"min-width:0;overflow:hidden;text-overflow:ellipsis;"
    "white-space:nowrap;text-align:right\">${esc(right)}</span></div></div>`;"
    "const ico=(inner,c)=>`<span style=\"position:relative;width:32px;height:32px;"
    "border-radius:9px;display:grid;place-items:center;flex:none;"
    "font:700 12px ${T.fn};background:${soft(c)};color:${c}\">${inner}</span>`;"
    "const item=(icon,name,sub,right,rc)=>`<div style=\"position:relative;"
    "display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:12px;"
    "align-items:center;padding:10px 14px\">${icon}<span style=\"position:relative;"
    "min-width:0\"><b style=\"display:block;font:600 13px ${T.sys};white-space:nowrap;"
    "overflow:hidden;text-overflow:ellipsis\">${esc(name)}</b>"
    "<span style=\"display:block;"
    "font:500 11.5px ${T.mono};color:${T.muted};white-space:nowrap;overflow:hidden;"
    "text-overflow:ellipsis\">${esc(sub)||'&nbsp;'}</span></span><span style=\""
    "position:relative;font:600 12.5px ${T.fn};font-variant-numeric:tabular-nums;"
    "color:${rc||T.text};white-space:nowrap\">${right}</span></div>`;"
)


def _tokens(th: dict) -> str:
    """Tokens du thème exposés au JS sous le nom `T`."""
    keys = ("text", "muted", "track", "accent", "in", "out", "ok", "warn", "bad",
            "bar_o", "name_track")
    tokens = {k: th[k] for k in keys}
    tokens.update(sys=_SYS, mono=_MONO, lf=th["font_label"], fn=th["font_num"],
                  fd=th["font_display"])
    return "const T=" + json.dumps(tokens, ensure_ascii=False) + ";"


def _js(th: dict, code: str, **subs: Any) -> str:
    """Gabarit button-card ; `@NOM@` est remplacé par la valeur JSON-échappée."""
    for key, value in subs.items():
        code = code.replace(f"@{key}@", json.dumps(value, ensure_ascii=False))
    return "[[[ " + _tokens(th) + _JS_LIB + code + " ]]]"


def _html(th: dict, js: str, watch: list[str], columns: Any = 12,
          entity: str | None = None, hero: bool = False,
          hide: str | None = None, **extra: Any) -> dict:
    """Carte button-card dont tout le contenu est un champ HTML `c`.

    `hide` : condition Jinja (card-mod) qui retire la carte de la mise en page.
    """
    card_style = [
        {"padding": "0"},
        {"background": th["hero_bg"] if hero else th["card"]},
        {"border": "1px solid " + (th["hero_line"] if hero else th["line"])},
        {"border-radius": f"{th['radius']}px"},
        {"box-shadow": th["shadow"]},
        {"overflow": "hidden"}, {"color": th["text"]},
        {"font-family": _SYS}, {"text-align": "left"},
    ]
    if th["blur"]:
        card_style += [{"backdrop-filter": th["blur"]},
                       {"-webkit-backdrop-filter": th["blur"]}]
    card: dict[str, Any] = {
        "type": "custom:button-card",
        "show_name": False, "show_icon": False, "show_state": False,
        "show_label": False,
        "triggers_update": watch,
        "custom_fields": {"c": js},
        "styles": {
            "card": card_style,
            "grid": [{"grid-template-areas": '"c"'},
                     {"grid-template-columns": "minmax(0, 1fr)"},
                     {"grid-template-rows": "auto"}],
            "custom_fields": {"c": [{"min-width": "0"}, {"width": "100%"}]},
        },
        "tap_action": {"action": "more-info"} if entity else {"action": "none"},
        "grid_options": {"columns": columns, "rows": "auto"},
    }
    if entity:
        card["entity"] = entity
    if hide:
        card["card_mod"] = {
            "style": "{% if " + hide + " %}"
            ":host { display: none !important; }{% endif %}"
        }
    card.update(extra)
    return card


# ---- Composants ----

def _heading(th: dict, title: str, **extra: Any) -> dict:
    """Titre de section : petites capitales précédées d'un carré d'accent."""
    card = {
        "type": "heading", "heading": title,
        "card_mod": {"style": (
            "ha-card { background: none; border: none; box-shadow: none; } "
            ".title { font-family: " + th["font_label"] + "; "
            "text-transform: uppercase; letter-spacing: 1.6px; "
            "font-size: 11px !important; font-weight: 600; color: "
            + th["muted"] + " !important; display: flex; align-items: center; "
            "gap: 8px; } .title::before { content: ''; width: 6px; height: 6px; "
            "border-radius: 2px; background: " + th["accent"] + "; flex: none; }"
        )},
    }
    card.update(extra)
    return card


def _kpi(th: dict, entity: str, name: str, tone: str, decimals: int) -> dict:
    """Tuile chiffre clé + sparkline sur la dernière heure (mini-graph-card)."""
    size = str(th["kpi_size"])
    style = _panel(th) + (
        ".state__value { font-family: " + th["font_num"] + "; font-weight: 700; "
        "letter-spacing: -0.5px; font-size: " + size + "px !important; } "
        ".state__uom { font-family: " + _SYS + "; opacity: 0.6; } "
        ".header .name { font-family: " + th["font_label"] + "; "
        "text-transform: uppercase; letter-spacing: 1.3px; font-size: 11px; "
        "font-weight: 600; color: " + th["muted"] + "; } "
    )
    if th["kpi_bar"]:
        style += "ha-card { box-shadow: inset 3px 0 0 " + tone + "; } "
    return {
        "type": "custom:mini-graph-card",
        "entities": [{"entity": entity, "color": tone}],
        "name": name, "hours_to_show": 1, "points_per_hour": 120,
        "line_width": 2, "height": 56, "decimals": decimals, "animate": True,
        "show": {"icon": False, "fill": "fade", "labels": False,
                 "extrema": False, "legend": False, "points": False},
        "grid_options": {"columns": 6},
        "card_mod": {"style": style},
    }


def _mini(th: dict, entity: str, label: str, value: str, tone: str | None = None,
          pct: str | None = None, columns: int = 12) -> dict:
    """Étiquette à gauche, valeur à droite, jauge arrondie en dessous.

    `value` / `pct` sont des expressions JS (la variable `v` vaut N(entité)).
    """
    meter = ("${meter(" + pct + ", @TONE@)}") if pct else ""
    js = _js(th, (
        "const v=N(@E@);return `<div style=\"padding:14px 16px\"><div style=\""
        "display:flex;justify-content:space-between;align-items:baseline;gap:10px\">"
        "${lab(@L@)}<b style=\"font:700 18px ${T.fn};font-variant-numeric:"
        "tabular-nums;white-space:nowrap\">${" + value + "}</b></div>" + meter
        + "</div>`;"
    ), E=entity, L=label, TONE=tone or th["accent"])
    return _html(th, js, [entity], columns, entity)


def _temp(th: dict, entity: str, label: str, warn_at: int, bad_at: int,
          sub: str) -> dict:
    """Température : gros chiffre coloré selon le seuil, jauge, sous-ligne."""
    js = _js(th, (
        "const v=N(@E@);const c=v>=@BAD@?T.bad:(v>=@WARN@?T.warn:T.ok);"
        "const sub=(()=>{" + sub + "})();"
        "return `<div style=\"padding:14px 16px;display:flex;flex-direction:column;"
        "gap:2px\">${lab(@L@)}<span style=\"font:700 24px ${T.fn};"
        "font-variant-numeric:tabular-nums;color:${c}\">${fr(v,1)}<small style=\""
        "font:500 12px ${T.sys};color:${T.muted};margin-left:3px\">°C</small></span>"
        "${meter(v,c)}<span style=\"font:500 11px ${T.sys};color:${T.muted};"
        "margin-top:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis\">"
        "${esc(sub)||'&nbsp;'}</span></div>`;"
    ), E=entity, L=label, WARN=warn_at, BAD=bad_at)
    return _html(th, js, [entity], 6, entity,
                 visibility=[{"condition": "state", "entity": entity,
                              "state_not": ["unknown", "unavailable"]}])


def _top_list(th: dict, entity: str, wan: str, tone: str, title: str,
              arrow: str) -> dict:
    """Classement : carré de rang teinté, nom + adresse, débit à droite,
    barre de fond au prorata du débit WAN total (`wan`, en Mbit/s), puis une
    ligne qui dit quelle part du trafic WAN les 5 premiers représentent.

    Le top vient d'une capture iftop de 2 s faite toutes les ~10 s : il ne
    retombe jamais pile sur le débit WAN, d'où la ligne de couverture."""
    rows: list[dict[str, Any]] = [{
        "type": "heading", "heading": title, "heading_style": "subtitle",
        "icon": arrow,
        "card_mod": {"style": "ha-icon { color: " + tone + " !important; }"},
    }]
    for i in range(TOP_ROWS):
        js = _js(th, (
            "const l=A(@E@,'top_5')||[];const d=l[@I@];if(!d)return '';"
            "const mx=Math.max(1,N(@W@)*1e6||0,...l.map((x)=>x.rate_bps||0));"
            "const r=d.rate_bps||0;"
            "const rate=r>=1e6?fr(r/1e6,1)+' Mbit/s':Math.round(r/1e3)+' kbit/s';"
            "return `<span style=\"position:absolute;inset:0 auto 0 0;"
            "width:${(r/mx*100).toFixed(1)}%;background:@TONE_RAW@;"
            "opacity:${T.bar_o}\"></span>`+item(ico(@I@+1,@TONE@),d.name,"
            "d.name!==d.address?d.address:'',rate);"
        ).replace("@TONE_RAW@", tone), E=entity, I=i, TONE=tone, W=wan)
        rows.append(_html(
            th, js, [entity, wan], 12, entity,
            hide="(state_attr('" + entity + "','top_5') or []) | count <= " + str(i),
        ))
    cover = _js(th, (
        "const l=A(@E@,'top_5')||[];const w=N(@W@);"
        "const t=l.reduce((a,x)=>a+(x.rate_bps||0),0)/1e6;"
        "if(!l.length||isNaN(w)||w<=0)return '';"
        "const p=Math.min(100,Math.round(t/w*100));"
        "return `<div style=\"padding:10px 14px;font:500 12px ${T.sys};"
        "color:${T.muted};display:flex;justify-content:space-between;gap:10px;"
        "flex-wrap:wrap\"><span>Top ${l.length} : <b style=\"font:600 12px "
        "${T.fn};color:${T.text}\">${fr(t,1)} Mbit/s</b> sur ${fr(w,1)} Mbit/s"
        " du WAN (${p} %)</span><span>relevé de 2 s toutes les ~10 s</span></div>`;"
    ), E=entity, W=wan)
    rows.append(_html(th, cover, [entity, wan], 12, wan))
    return {"type": "vertical-stack", "cards": rows,
            "grid_options": {"columns": 12}}


def _tunnels(th: dict, entity: str) -> dict:
    """Une ligne par tunnel VPN, dans une pile verticale.

    La pile est indispensable : dans une section, une carte masquée garde sa
    cellule de grille (trou visible), alors qu'une pile la retire vraiment.
    """
    cards = []
    for i in range(TUNNEL_SLOTS):
        js = _js(th, (
            "const t=(A(@E@,'tunnels')||[])[@I@];if(!t)return '';"
            "const c=t.up?T.ok:T.bad;"
            "const nm=String(t.name||t.device).toLowerCase()"
            ".replace(/\\b\\w/g,(m)=>m.toUpperCase())"
            ".replace(/Wireguard/g,'WireGuard').replace(/Openvpn/g,'OpenVPN')"
            ".replace(/Ipsec/g,'IPsec').replace(/\\bVpn\\b/g,'VPN')"
            ".replace(/\\bVti\\b/g,'VTI');"
            "return item(ico('●',c),nm,`${t.kind} · ${t.address||'–'}"
            "${t.remote?' → '+t.remote:''}`,t.up?'En ligne':'Coupé',c);"
        ), E=entity, I=i)
        cards.append(_html(
            th, js, [entity], 12, entity,
            hide="(state_attr('" + entity + "','tunnels') or []) | count <= " + str(i),
        ))
    return {"type": "vertical-stack", "cards": cards,
            "grid_options": {"columns": 12}}


def _link_card(th: dict, link: dict, e: dict[str, str], main: str,
               columns: int) -> dict:
    """Carte d'un lien WAN : état, latence, pertes, débits entrant / sortant.

    `main` = capteur de latence globale, dont l'attribut `wans` dit quel lien
    porte la route par défaut. « En secours » = en ligne, hors route par
    défaut et sans trafic notable.
    """
    pre = f"wan_{link['id']}_"
    up, lat, loss = (e.get(pre + "connected"), e.get(pre + "latency"),
                     e.get(pre + "packet_loss"))
    rin, rout = e.get(pre + "throughput_in"), e.get(pre + "throughput_out")
    watch = [x for x in (up, lat, loss, rin, rout, main) if x]
    js = _js(th, (
        "const w=(A(@MAIN@,'wans')||[]).find((x)=>x.id===@ID@)||{};"
        "const on=S(@UP@)==='on',off=S(@UP@)==='off';"
        "const li=N(@RIN@),lo=N(@ROUT@),la=N(@LAT@),ls=N(@LOSS@);"
        "const idle=on&&!w.default&&!((li||0)+(lo||0)>0.1);"
        "const c=off?T.bad:(idle?T.muted:T.ok);"
        "const ch=states[@UP@]?new Date(states[@UP@].last_changed):null;"
        "const hm=ch?ch.toLocaleTimeString('fr-FR',{hour:'2-digit',"
        "minute:'2-digit'}):'';"
        "const st=off?'Coupé'+(hm?' depuis '+hm:''):(idle?'En secours':"
        "(on?'En ligne':'Inconnu'));"
        "const mx=Math.max(10,li||0,lo||0);"
        "const rate=(l,v,col)=>`<div style=\"display:grid;grid-template-columns:"
        "64px minmax(0,1fr) 84px;gap:10px;align-items:center;font:500 12px "
        "${T.sys};color:${T.muted}\"><span>${l}</span><div style=\"height:6px;"
        "border-radius:99px;background:${T.track};overflow:hidden\"><i style=\""
        "display:block;height:100%;width:${isNaN(v)?0:Math.max(v>0?2:0,"
        "Math.min(100,v/mx*100))}%;border-radius:inherit;background:${col}\">"
        "</i></div><span style=\"text-align:right;font:600 12.5px ${T.fn};"
        "font-variant-numeric:tabular-nums;color:${off?T.muted:T.text}\">"
        "${isNaN(v)?'–':(v<1?fr(v*1000,0)+' kb/s':fr(v,1)+' Mb/s')}</span></div>`;"
        "const kpi=(l,v,u,p,col)=>`<div style=\"min-width:0\">${lab(l)}<div "
        "style=\"font:700 22px ${T.fn};font-variant-numeric:tabular-nums;"
        "margin-top:2px;color:${off?T.muted:T.text}\">${isNaN(v)?'–':fr(v,1)}"
        "<small style=\"font:500 12px ${T.sys};color:${T.muted};margin-left:3px\">"
        "${u}</small></div>${meter(p,col)}</div>`;"
        "const lc=la>60?T.warn:T.ok;const sc=ls>=100?T.bad:(ls>2?T.warn:T.ok);"
        "return `<div style=\"padding:14px 16px;display:grid;gap:12px;"
        "${off?'box-shadow:inset 0 0 0 1px '+soft(T.bad,45)"
        "+';border-radius:inherit;':''}"
        "\"><div style=\"display:flex;align-items:flex-start;gap:10px\">"
        "<div style=\"min-width:0\"><b style=\"font:600 15px ${T.sys}\">"
        "${esc(@NAME@)}</b>${w.default?`<span style=\"font:600 10.5px ${T.sys};"
        "color:${T.accent};border:1px solid ${soft(T.accent,50)};border-radius:6px;"
        "padding:1px 6px;margin-left:8px;vertical-align:2px\">défaut</span>`:''}"
        "<div style=\"font:500 11.5px ${T.mono};color:${T.muted};margin-top:2px;"
        "white-space:nowrap;overflow:hidden;text-overflow:ellipsis\">"
        "${esc(@DEV@)} · ${esc(w.gateway||@GW@)}</div></div>"
        "<span style=\"margin-left:auto\">${pill(st,c)}</span></div>"
        "<div style=\"display:grid;grid-template-columns:repeat(2,minmax(0,1fr));"
        "gap:12px\">${kpi('Latence',la,'ms',Math.min(100,la||0),lc)}"
        "${kpi('Pertes',ls,'%',ls,sc)}</div><div style=\"display:grid;gap:8px\">"
        "${rate('Entrant',li,T.in)}${rate('Sortant',lo,T.out)}</div></div>`;"
    ), MAIN=main, ID=link["id"], UP=up or "", RIN=rin or "", ROUT=rout or "",
        LAT=lat or "", LOSS=loss or "", NAME=link["name"],
        DEV=link.get("device") or link["id"], GW=link.get("gateway") or "")
    return _html(th, js, watch, columns, up)


def _group_card(th: dict, entity: str, name: str, columns: int) -> dict:
    """Carte d'un groupe de passerelles : membres par niveau, trafic actif."""
    js = _js(th, (
        "const tiers=A(@E@,'tiers')||[];const act=A(@E@,'active')||[];"
        "const tr={down:'bascule si membre down',downloss:'bascule sur pertes',"
        "downlatency:'bascule sur latence',downlosslatency:"
        "'bascule sur pertes ou latence'}[A(@E@,'trigger')]||'';"
        "const desc=[A(@E@,'description'),tr].filter((x)=>x).join(' · ');"
        "const head=act.length?pill('Trafic sur '+esc(act.join(' + ')),T.ok):"
        "pill('Hors ligne',T.bad);"
        "const mem=(m)=>{const a=act.includes(m.name);"
        "const c=m.usable?T.ok:(m.status?T.bad:T.muted);"
        "return `<span style=\"display:inline-flex;align-items:center;gap:8px;"
        "padding:6px 10px;border-radius:8px;font:500 12px ${T.mono};border:1px solid "
        "${a?T.accent:T.track};${a?'box-shadow:inset 0 0 0 1px '+T.accent+';':''}"
        "\"><i style=\"width:7px;height:7px;border-radius:50%;background:${c}\">"
        "</i>${esc(m.name)}${a?`<span style=\"font:600 11px ${T.sys};"
        "color:${T.accent}\">trafic</span>`:''}</span>`;};"
        "const rows=tiers.map((t)=>`<div style=\"display:grid;grid-template-columns:"
        "64px minmax(0,1fr);gap:10px;align-items:center\">${lab('Niveau '+t.tier)}"
        "<div style=\"display:flex;flex-wrap:wrap;gap:8px\">"
        "${(t.gateways||[]).map(mem).join('')}</div></div>`).join('');"
        "return `<div style=\"padding:14px 16px;display:grid;gap:12px\">"
        "<div style=\"display:flex;align-items:flex-start;gap:10px;flex-wrap:wrap\">"
        "<div style=\"min-width:0\"><b style=\"font:600 15px ${T.sys}\">Groupe "
        "${esc(@NAME@)}</b><div style=\"font:500 12px ${T.sys};color:${T.muted};"
        "margin-top:2px\">${esc(desc)||'&nbsp;'}</div></div><span style=\""
        "margin-left:auto\">${head}</span></div><div style=\"display:grid;gap:8px\">"
        "${rows}</div></div>`;"
    ), E=entity, NAME=name)
    return _html(th, js, [entity], columns, entity)


def _wan_topology(hass: HomeAssistant | None, entry: ConfigEntry,
                  e: dict[str, str]) -> tuple[list[dict], list[tuple[str, str]]]:
    """Liens WAN qui ont leurs entités (multi-WAN) et groupes de passerelles."""
    hub = (hass.data.get(DOMAIN, {}).get(entry.entry_id)
           if hass is not None else None)
    data = (getattr(getattr(hub, "fast", None), "data", None) or {})
    links = [link for link in links_with_entities(data)
             if f"wan_{link['id']}_connected" in e]
    groups = [(e[f"gateway_group_{g['name']}"], g["name"])
              for g in data.get("_wan_groups") or []
              if f"gateway_group_{g['name']}" in e]
    return links, groups


def _topology_key(links: list[dict], groups: list[tuple[str, str]]) -> list[str]:
    """Signature des liens / groupes affichés (re-semis quand elle change)."""
    return [f"wan:{link['id']}" for link in links] + [f"group:{g}" for _, g in groups]


# Rendu "legacy" du gabarit v14 (sans les deux esc() ajoutés en v15) : sert à
# prouver qu'un dashboard semé par 2.3.8 (sans empreinte) n'a pas été retouché.
LEGACY_TEMPLATE_VERSION = 14
_LEGACY_V14_SUBS = (
    ("pill('Trafic sur '+esc(act.join(' + ')),T.ok)",
     "pill('Trafic sur '+act.join(' + '),T.ok)"),
    ("'En ligne · '+esc(down.map((w)=>w.name).join(', '))+' coupé'",
     "'En ligne · '+down.map((w)=>w.name).join(', ')+' coupé'"),
)


def _to_legacy_v14(node: Any) -> Any:
    """Défait récursivement les deux esc() de la v15 dans une config rendue."""
    if isinstance(node, str):
        for new, old in _LEGACY_V14_SUBS:
            node = node.replace(new, old)
        return node
    if isinstance(node, list):
        return [_to_legacy_v14(x) for x in node]
    if isinstance(node, dict):
        return {k: _to_legacy_v14(v) for k, v in node.items()}
    return node


def _build_dashboard_config(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    legacy_v14: bool = False,
    theme: str | None = None,
) -> dict:
    """Construit la config lovelace dans le thème choisi.

    `legacy_v14` rend le gabarit tel que livré en v14 (version incluse) et
    `theme` impose le thème (celui stocké) : usage réservé à la preuve
    "dashboard 2.3.8 non retouché".
    """
    theme_name, th = _theme(entry)
    if theme in THEMES:
        theme_name, th = theme, THEMES[theme]
    e = _entity_map(hass, entry)

    def s(key: str) -> str:
        return e.get(key, f"sensor.unknown_{key}")

    wan = s("wan_connected")

    # ---- Bandeau d'état ----
    hero_js = _js(th, (
        "const up=S(@WAN@)==='on';const live=!isNaN(N(@CPU@));"
        "const ws=A(@LAT@,'wans')||[];const multi=ws.length>1;"
        "const down=ws.filter((w)=>w.online===false);"
        "const via=(ws.find((w)=>w.default)||{}).name;"
        "const c=!up?T.bad:(multi&&down.length?T.warn:T.ok);"
        "const host=S(@HOST@).split('.')[0].toUpperCase();"
        "const ver=S(@VER@).split('-')[0];"
        # Uptime OPNsense : "2 days, 02:41:10" / "1 day, 03:00:00" / "02:41:10".
        "const p=S(@UPT@).split(', ');const h=p[p.length-1].split(':');"
        "const upt=h.length===3?(p.length>1?p[0].split(' ')[0]+' j ':'')"
        "+parseInt(h[0],10)+' h '+h[1]:'–';"
        "return `<style>@keyframes opnp{50%{opacity:.25}}"
        ".lv{animation:opnp 1.6s ease-in-out infinite}"
        "@media (prefers-reduced-motion:reduce){.lv{animation:none}}</style>"
        "<div style=\"display:flex;align-items:center;gap:16px;padding:18px 20px\">"
        "<div style=\"width:46px;height:46px;border-radius:12px;display:grid;"
        "place-items:center;flex:none;background:${soft(c)};color:${c}\">"
        "<ha-icon icon=\"${up?'mdi:shield-check':'mdi:shield-alert'}\" "
        "style=\"--mdc-icon-size:24px\"></ha-icon></div><div style=\"min-width:0\">"
        "<div style=\"font:700 21px ${T.fd};letter-spacing:${T.name_track};"
        "line-height:1.25\">${esc(host)}</div><div style=\"display:flex;"
        "flex-wrap:wrap;align-items:center;gap:6px 14px;margin-top:4px;"
        "font:500 12.5px ${T.mono};color:${T.muted}\">"
        "${pill(!up?(multi?'Internet coupé':'WAN coupé'):(multi&&down.length?"
        "'En ligne · '+esc(down.map((w)=>w.name).join(', '))+' coupé':'En ligne'),c)}"
        "${live?pill('Temps réel',T.accent,true):''}"
        "${multi&&up&&via?`<span>via ${esc(via)}</span>`:''}"
        "<span>OPNsense ${esc(ver)}</span><span>${esc(S(@IP@))}</span>"
        "<span>up ${upt}</span></div></div></div>`;"
    ), WAN=wan, CPU=s("cpu_usage"), HOST=s("hostname"),
        VER=s("opnsense_version"), UPT=s("uptime"), IP=s("public_ipv4"),
        LAT=s("wan_latency"))
    hero = _html(th, hero_js, [wan, s("cpu_usage"), s("hostname"),
                               s("opnsense_version"), s("uptime"), s("public_ipv4"),
                               s("wan_latency")],
                 "full", wan, hero=True)

    # ---- Liens WAN (multi-WAN uniquement) ----
    links, groups = _wan_topology(hass, entry, e)
    wan_section: list[dict] = []
    if links:
        count = len(links) + len(groups)
        columns = 12 if count >= 3 else 18
        wan_section = [{"type": "grid", "column_span": 3, "cards": [
            _heading(th, "Liens WAN"),
            *(_link_card(th, link, e, s("wan_latency"), columns) for link in links),
            *(_group_card(th, ent, name, columns) for ent, name in groups),
        ]}]

    # ---- Temps réel ----
    def serie(entity: str, name: str, color: str) -> dict:
        return {"entity": entity, "name": name, "type": "area",
                "color": color, "stroke_width": 2, "opacity": 0.16,
                # Pic par tranche de 5 min : une moyenne écraserait un burst
                # de quelques secondes (speedtest, téléchargement).
                "group_by": {"func": "max", "duration": "5min"},
                # La légende donnerait le pic des 5 dernières minutes, pris
                # pour le débit actuel : le chiffre courant est dans les tuiles.
                "show": {"legend_value": False}}

    chart = {
        "type": "custom:apexcharts-card", "graph_span": "24h",
        "header": {"show": True, "title": "TRAFIC WAN · 24 H · PICS PAR 5 MIN"},
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
            "letter-spacing: 1.4px; font-size: 11px; font-weight: 600; color: "
            + th["muted"] + "; }"
        )},
    }
    live = {"type": "grid", "column_span": 2, "cards": [
        _heading(th, "Temps réel"),
        _kpi(th, s("wan_throughput_in"), "↓ Entrant", th["in"], 1),
        _kpi(th, s("wan_throughput_out"), "↑ Sortant", th["out"], 1),
        _kpi(th, s("wan_latency"), "Latence", th["ok"], 1),
        _kpi(th, s("cpu_usage"), "CPU", th["warn"], 0),
        chart,
    ]}

    # ---- Colonne de droite : connexion, températures, système ----
    services = s("services_stopped")
    services_js = _js(th, (
        "const n=N(@E@);const run=A(@E@,'running'),tot=A(@E@,'total');"
        "const stop=A(@E@,'stopped')||[];"
        "if(isNaN(n))return row2('Services',pill('Droits API',T.warn),"
        "'Privilège Status: Services requis','');"
        "if(n===0)return row2('Services',pill('Tout tourne',T.ok),"
        "`${run} / ${tot} actifs`,'');"
        "return row2('Services',pill(n+(n>1?' arrêtés':' arrêté'),T.bad),"
        "`${run} / ${tot} actifs`,stop.join(', '));"
    ), E=services)
    firmware_js = _js(th, (
        "const up=S(@UPD@)==='on';"
        "return row2('Firmware',up?pill('Mise à jour',T.warn):pill('À jour',T.ok),"
        "esc(S(@INST@)),up?S(@LAST@)+' disponible':'appui long : vérifier');"
    ), UPD=s("update_available"), INST=s("firmware_installed"),
        LAST=s("firmware_latest"))

    side = {"type": "grid", "cards": [
        _heading(th, "Connexion"),
        _mini(th, s("wan_packet_loss"), "Pertes de paquets", "fr(v,1)+' %'",
              th["ok"], "v*10"),
        _html(th, services_js, [services], 12, services),
        _html(th, firmware_js,
              [s("update_available"), s("firmware_installed"), s("firmware_latest")],
              12, s("firmware_update"),
              hold_action={"action": "perform-action",
                           "perform_action": "button.press",
                           "target": {"entity_id": s("check_updates")}}),
        _heading(th, "Températures"),
        _temp(th, s("temp_cpu"), "CPU", 60, 75, (
            "const n=(A(@E@,'sensors')||[]).filter((x)=>x.type==='cpu').length;"
            "return n>1?`max des ${n} cœurs`:'sonde processeur';"
        ).replace("@E@", json.dumps(s("temp_cpu")))),
        _temp(th, s("temp_sfp"), "SFP", 50, 65, (
            "const m=(A(@E@,'modules')||[])[0];"
            "return m?`module ${m.device}${m.interface?' · '+m.interface:''}`:'';"
        ).replace("@E@", json.dumps(s("temp_sfp")))),
        _heading(th, "Système"),
        _mini(th, s("ram_used_percent"), "RAM", "fr(v,1)+' %'", th["out"], "v"),
        _mini(th, s("disk_root_percent"), "Disque", "fr(v,v<10?1:0)+' %'",
              th["in"], "v"),
    ]}

    # ---- Top destinations ----
    top = {"type": "grid", "column_span": 2, "cards": [
        _heading(th, "Top destinations"),
        _top_list(th, s("wan_top_dest_in"), s("wan_throughput_in"), th["in"],
                  "Entrant", "mdi:arrow-down"),
        _top_list(th, s("wan_top_dest_out"), s("wan_throughput_out"), th["out"],
                  "Sortant", "mdi:arrow-up"),
    ]}

    # ---- Tunnels VPN + volumes WAN ----
    tunnels = s("vpn_tunnels_up")
    vpn = {"type": "grid", "cards": [
        _heading(th, "Tunnels VPN",
                 badges=[{"type": "entity", "entity": tunnels,
                          "show_state": True, "show_icon": False}]),
        _tunnels(th, tunnels),
        _heading(th, "Volumes WAN"),
        _mini(th, s("wan_total_received"), "↓ Reçu", "fr(v,1)+' Go'", columns=6),
        _mini(th, s("wan_total_transmitted"), "↑ Transmis", "fr(v,1)+' Go'",
              columns=6),
    ]}

    config = {
        "title": "OPNsense",
        "template_version": DASHBOARD_TEMPLATE_VERSION,
        "theme": theme_name,
        "wan_topology": _topology_key(links, groups),
        "views": [{
            "title": "Pare-feu", "path": "pare-feu", "type": "sections",
            "max_columns": 3,
            "background": th["background"],
            "sections": [
                {"type": "grid", "column_span": 3, "cards": [hero]},
                *wan_section,
                # Deux colonnes qui s'écoulent chacune de leur côté : une
                # section par rangée laisserait un trou sous la plus courte
                # (le top destinations attendait le bas de la colonne Système).
                {"type": "grid", "column_span": 2,
                 "cards": live["cards"] + top["cards"]},
                {"type": "grid", "cards": side["cards"] + vpn["cards"]},
            ],
        }],
    }
    if legacy_v14:
        config = _to_legacy_v14(config)
        config["template_version"] = LEGACY_TEMPLATE_VERSION
    return config


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


# ---- Registre persistant des dashboards ----
# Store `opnsense_custom.dashboards` : entry_id -> {url_path, seeded_hash}.
# Il fige l'URL de chaque firewall (elle ne dépend plus du rang de l'entrée) et
# mémorise l'empreinte de la config au moment du semis, pour détecter les
# retouches manuelles.
_REGISTRY_VERSION = 1
_K_REGISTRY = "_dash_registry"
_K_STORE = "_dash_store"
_K_LOCK = "_dash_lock"
_K_WATCH = "_dash_watch"
_K_WARNED = "_dash_import_warned"
# Anti-rebond (s) du re-semis après un renommage d'entité.
RENAME_DEBOUNCE = 10


def _rank_url(hass: HomeAssistant, entry: ConfigEntry) -> str:
    """URL historique (par rang) : "opnsense", "opnsense-2", "opnsense-3"..."""
    entry_ids = [e.entry_id for e in hass.config_entries.async_entries(DOMAIN)]
    rank = entry_ids.index(entry.entry_id) + 1 if entry.entry_id in entry_ids else 1
    return DASHBOARD_URL_PATH if rank == 1 else f"{DASHBOARD_URL_PATH}-{rank}"


async def _async_registry(hass: HomeAssistant) -> dict[str, dict[str, Any]]:
    """Registre persistant (chargé une fois ; amorcé à la première utilisation).

    Amorçage : sans fichier, l'URL calculée par rang est figée pour chaque
    entrée existante, afin qu'aucune URL déjà en service ne change.
    """
    domain_data = hass.data.setdefault(DOMAIN, {})
    lock = domain_data.setdefault(_K_LOCK, asyncio.Lock())
    async with lock:
        registry = domain_data.get(_K_REGISTRY)
        if registry is None:
            store = domain_data.setdefault(
                _K_STORE, Store(hass, _REGISTRY_VERSION, f"{DOMAIN}.dashboards")
            )
            raw = await store.async_load()
            if raw is None:
                registry = {
                    e.entry_id: {"url_path": _rank_url(hass, e)}
                    for e in hass.config_entries.async_entries(DOMAIN)
                }
                await store.async_save({"dashboards": registry})
            else:
                registry = dict(raw.get("dashboards", {}))
            domain_data[_K_REGISTRY] = registry
    return registry


async def _async_save_registry(hass: HomeAssistant) -> None:
    """Écrit le registre sur disque."""
    domain_data = hass.data.get(DOMAIN, {})
    store = domain_data.get(_K_STORE)
    registry = domain_data.get(_K_REGISTRY)
    if store is not None and registry is not None:
        await store.async_save({"dashboards": registry})


async def _async_record(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """Enregistrement de CE firewall ; en crée un (premier URL libre) si absent."""
    registry = await _async_registry(hass)
    rec = registry.get(entry.entry_id)
    if rec is None:
        taken = {r.get("url_path") for r in registry.values()}
        url_path, n = DASHBOARD_URL_PATH, 1
        while url_path in taken:
            n += 1
            url_path = f"{DASHBOARD_URL_PATH}-{n}"
        rec = registry[entry.entry_id] = {"url_path": url_path}
        await _async_save_registry(hass)
    return rec


def _title_for(url_path: str) -> str:
    """Titre de sidebar déduit de l'URL ("opnsense-2" -> "OPNsense 2")."""
    if url_path == DASHBOARD_URL_PATH:
        return "OPNsense"
    return f"OPNsense {url_path.rsplit('-', 1)[-1]}"


def _config_hash(config: Any) -> str:
    """Empreinte stable d'une config lovelace (détection des retouches)."""
    # Aller-retour JSON : mêmes types natifs que la config relue du disque.
    native = json.loads(json.dumps(config))
    raw = json.dumps(native, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _async_seed(
    hass: HomeAssistant, entry: ConfigEntry, storage: Any, rec: dict[str, Any]
) -> None:
    """Sème le gabarit, mémorise l'empreinte de la config, ferme l'alerte."""
    await storage.async_save(_build_dashboard_config(hass, entry))
    # Empreinte de ce que la storage sert réellement après le semis.
    rec["seeded_hash"] = _config_hash(await storage.async_load(force=False))
    await _async_save_registry(hass)
    delete_dashboard_issue(hass, entry)
    _LOGGER.debug("Dashboard OPNsense semé/rafraîchi (v%s)",
                  DASHBOARD_TEMPLATE_VERSION)


@callback
def _async_watch_renames(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Re-sème (avec anti-rebond) quand une entité de l'entrée est renommée.

    Le re-semis passe par la même protection que les autres : un dashboard
    retouché n'est pas écrasé (alerte Réparations à la place).
    """
    watched: set[str] = hass.data.setdefault(DOMAIN, {}).setdefault(_K_WATCH, set())
    if entry.entry_id in watched:
        return
    watched.add(entry.entry_id)
    timer: list[Any] = []

    async def _refresh(_now: Any) -> None:
        timer.clear()
        await async_register_dashboard(hass, entry, refresh=True)

    @callback
    def _on_registry_updated(event: Event) -> None:
        data = event.data
        if (data.get("action") != "update"
                or "entity_id" not in (data.get("changes") or {})):
            return
        reg_entry = er.async_get(hass).async_get(data.get("entity_id"))
        if reg_entry is None or reg_entry.config_entry_id != entry.entry_id:
            return
        if timer:
            timer.pop()()
        timer.append(async_call_later(hass, RENAME_DEBOUNCE, _refresh))

    @callback
    def _cleanup() -> None:
        watched.discard(entry.entry_id)
        if timer:
            timer.pop()()

    entry.async_on_unload(
        hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, _on_registry_updated)
    )
    entry.async_on_unload(_cleanup)


async def async_register_dashboard(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    force: bool = False,
    refresh: bool = False,
) -> None:
    """Pose (ou rafraîchit) le dashboard OPNsense dans la sidebar. Best-effort.

    `refresh` force un re-semis (renommage d'entité) ; `force` le fait même sur
    un dashboard retouché (confirmation de l'utilisateur dans Réparations).
    Sans `force`, un dashboard retouché n'est JAMAIS écrasé.
    """
    if not entry.options.get(CONF_CREATE_DASHBOARD, DEFAULT_CREATE_DASHBOARD):
        # Dashboard désactivé : l'alerte de réinitialisation n'a plus d'objet.
        delete_dashboard_issue(hass, entry)
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
    except ImportError as err:
        # LOVELACE_DATA n'existe qu'à partir de HA 2025.2.0 : une seule fois.
        domain_data = hass.data.setdefault(DOMAIN, {})
        if not domain_data.get(_K_WARNED):
            domain_data[_K_WARNED] = True
            _LOGGER.warning(
                "Dashboard OPNsense ignoré : API lovelace introuvable (%s). "
                "Home Assistant 2025.2.0 ou plus récent est requis.", err
            )
        return

    try:
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

        rec = await _async_record(hass, entry)
        url_path = rec["url_path"]
        item = {
            "id": url_path, "url_path": url_path, "title": _title_for(url_path),
            "icon": "mdi:shield-lock", "show_in_sidebar": True,
            "require_admin": False,
        }

        storage = lovelace_data.dashboards.get(url_path)
        if storage is None:
            storage = lovelace_dashboard.LovelaceStorage(hass, item)
            lovelace_data.dashboards[url_path] = storage

        try:
            current = await storage.async_load(force=False)
        except Exception:  # noqa: BLE001 - ConfigNotFound & co.
            current = None
        # Re-sème si le gabarit a évolué, si l'utilisateur a changé de thème, si
        # un lien WAN / groupe de passerelles est apparu ou a disparu, ou sur
        # demande (renommage d'entité).
        topo = _topology_key(*_wan_topology(hass, entry, _entity_map(hass, entry)))
        if current and rec.get("seeded_hash") is None:
            # Semé par une version sans registre (2.3.8) : pas d'empreinte. Il
            # est prouvé intact s'il est identique au rendu legacy v14 (avec le
            # thème stocké) ; sinon il reste "inconnu" donc protégé.
            if current.get("template_version") == LEGACY_TEMPLATE_VERSION and (
                _config_hash(current) == _config_hash(_build_dashboard_config(
                    hass, entry, legacy_v14=True, theme=current.get("theme")))
            ):
                rec["seeded_hash"] = _config_hash(current)
                await _async_save_registry(hass)
        if not current:
            # Absent ou vide : rien à protéger.
            await _async_seed(hass, entry, storage, rec)
        elif (refresh
                or current.get("template_version", 0) < DASHBOARD_TEMPLATE_VERSION
                or current.get("theme") != _theme(entry)[0]
                or current.get("wan_topology", []) != topo):
            if force or rec.get("seeded_hash") == _config_hash(current):
                await _async_seed(hass, entry, storage, rec)
            else:
                # Retouché (ou semé avant l'existence du registre, donc
                # invérifiable) : on n'écrase pas, on prévient.
                create_dashboard_issue(hass, entry, url_path)
                _LOGGER.info(
                    "Dashboard %s modifié manuellement : mise à jour du "
                    "gabarit proposée dans Réparations", url_path
                )

        _register_panel(hass, url_path, MODE_STORAGE, item, update=True)
        _async_watch_renames(hass, entry)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning(
            "Création du dashboard OPNsense impossible (non bloquant): %s", err
        )


async def async_unregister_dashboard(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Retire le panneau de la sidebar (sans supprimer la config stockée)."""
    url_path = (
        hass.data.get(DOMAIN, {}).get(_K_REGISTRY, {})
        .get(entry.entry_id, {}).get("url_path")
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
    """Supprime définitivement le dashboard (config, enregistrement, alerte).

    Fonctionne même si l'entrée n'a pas été chargée pendant la session : l'URL
    vient du registre persistant.
    """
    delete_dashboard_issue(hass, entry)
    registry = await _async_registry(hass)
    rec = registry.pop(entry.entry_id, None)
    url_path = rec.get("url_path") if rec else None
    if rec is not None:
        await _async_save_registry(hass)
    # Sans enregistrement : l'entrée n'a jamais eu de dashboard (l'amorçage
    # couvre toutes les entrées antérieures), on ne supprime rien.
    if not url_path:
        return
    try:
        from homeassistant.components import frontend  # noqa: PLC0415
        from homeassistant.components.lovelace import LOVELACE_DATA  # noqa: PLC0415
        from homeassistant.components.lovelace import (  # noqa: PLC0415
            dashboard as lovelace_dashboard,
        )

        frontend.async_remove_panel(hass, url_path)
        lovelace_data = hass.data.get(LOVELACE_DATA)
        if lovelace_data is not None:
            storage = lovelace_data.dashboards.pop(url_path, None)
            if storage is None:
                # Entrée non chargée : on ouvre le fichier pour le supprimer.
                storage = lovelace_dashboard.LovelaceStorage(
                    hass, {"id": url_path, "url_path": url_path}
                )
            if hasattr(storage, "async_delete"):
                await storage.async_delete()
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Suppression du dashboard %s: %s", url_path, err)
