"""Alertes intégrées OPNsense (étape "Notifications" des options).

Évalue l'état du pare-feu à chaque rafraîchissement du coordinator et envoie
des notifications (persistante HA + services notify choisis) sur les
transitions : WAN coupé / rétabli (par lien en multi-WAN, et « internet
coupé » quand tous les liens sont tombés), bascule d'un groupe de
passerelles, latence élevée, mise à jour firmware, service arrêté, tunnel
VPN coupé / rétabli, disque presque plein, température.

Principes :
  * Basé sur les données du coordinator, pas sur les entity_id -> insensible
    aux renommages d'entités.
  * Le premier rafraîchissement sert de référence : un état déjà dégradé au
    démarrage de HA (service arrêté volontairement...) n'est pas notifié.
  * Le message "WAN rétabli" porte la durée de la coupure : pendant la
    coupure, une notification push ne peut pas sortir (hors local push).
  * Best-effort : un échec d'envoi est loggé en debug, jamais bloquant.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .binary_sensor import _has_update_available, _wan_up
from .const import (
    ALERT_DISK,
    ALERT_FIRMWARE,
    ALERT_LATENCY,
    ALERT_SERVICES,
    ALERT_TEMPERATURE,
    ALERT_VPN,
    ALERT_WAN,
    CONF_ALERTS,
    CONF_LATENCY_DURATION,
    CONF_LATENCY_THRESHOLD,
    CONF_NOTIFY_PERSISTENT,
    CONF_NOTIFY_TARGETS,
    CONF_TEMP_THRESHOLD,
    CONF_WAN_DOWN_DELAY,
    DEFAULT_LATENCY_DURATION,
    DEFAULT_LATENCY_THRESHOLD,
    DEFAULT_NOTIFY_PERSISTENT,
    DEFAULT_TEMP_THRESHOLD,
    DEFAULT_WAN_DOWN_DELAY,
    DISK_ALERT_PCT,
    DISK_REARM_PCT,
    TEMP_REARM_DELTA,
)
from .coordinator import OPNsenseDataCoordinator
from .sensor import (
    _firmware_latest,
    _get,
    _root_disk_used_percent,
    _services_attributes,
    _temp_cpu,
    _tunnels,
    _wan_latency,
)
from .wans import default_link

_LOGGER = logging.getLogger(__name__)


def _fmt_duration(seconds: float) -> str:
    """Durée lisible : "45 s", "12 min", "2 h 05"."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60:02d}"


def _hhmm(moment: datetime) -> str:
    return dt_util.as_local(moment).strftime("%H:%M")


class _Outage:
    """Suivi d'une coupure : début, alerte envoyée après le délai de grâce."""

    def __init__(self) -> None:
        self.since: datetime | None = None
        self.alerted = False
        # Coupure qui vient de se terminer (pour le message « rétabli »).
        self.last_since: datetime | None = None
        self.last_elapsed = 0.0

    def update(self, up: bool, now: datetime, delay: float) -> str | None:
        """Renvoie "down" / "up" quand il faut notifier, sinon None."""
        if not up:
            if self.since is None:
                self.since = now
            if not self.alerted and (now - self.since).total_seconds() >= delay:
                self.alerted = True
                return "down"
            return None
        if self.since is None:
            return None
        self.last_since = self.since
        self.last_elapsed = (now - self.since).total_seconds()
        notify = self.alerted or self.last_elapsed >= delay
        self.since, self.alerted = None, False
        return "up" if notify else None


class OPNsenseAlerts:
    """Machine à états des alertes d'un pare-feu."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: OPNsenseDataCoordinator,
    ) -> None:
        """Lit les options d'alerte de l'entry."""
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        options = entry.options
        self.enabled: set[str] = set(options.get(CONF_ALERTS) or [])
        self.targets: list[str] = list(options.get(CONF_NOTIFY_TARGETS) or [])
        self.persistent: bool = options.get(
            CONF_NOTIFY_PERSISTENT, DEFAULT_NOTIFY_PERSISTENT
        )
        self.wan_delay = 60 * options.get(
            CONF_WAN_DOWN_DELAY, DEFAULT_WAN_DOWN_DELAY
        )
        self.latency_threshold = options.get(
            CONF_LATENCY_THRESHOLD, DEFAULT_LATENCY_THRESHOLD
        )
        self.latency_duration = 60 * options.get(
            CONF_LATENCY_DURATION, DEFAULT_LATENCY_DURATION
        )
        self.temp_threshold = options.get(
            CONF_TEMP_THRESHOLD, DEFAULT_TEMP_THRESHOLD
        )
        # État interne
        self._baseline_done = False
        self._internet = _Outage()
        self._links: dict[str, _Outage] = {}
        self._groups_active: dict[str, tuple[str, ...]] = {}
        self._default_link: str | None = None
        self._latency_high_since: datetime | None = None
        self._latency_alerted = False
        self._update_available: bool | None = None
        self._stopped_services: set[str] = set()
        self._tunnels_up: dict[str, bool] = {}
        self._disk_alerted = False
        self._temp_alerted = False

    @property
    def active(self) -> bool:
        """True si au moins une alerte est cochée."""
        return bool(self.enabled)

    # ------------------------------------------------------------------
    @callback
    def handle_update(self) -> None:
        """Listener du coordinator : évalue les transitions."""
        # Vue fusionnée : WAN / latence / tunnels au rythme rapide, le reste lent.
        data = self.coordinator.merged if self.coordinator.data else None
        if not data or not self.active:
            return
        now = dt_util.utcnow()
        if not self._baseline_done:
            self._take_baseline(data, now)
            return
        for title, message, tag in self._evaluate(data, now):
            self.hass.async_create_task(self._send(title, message, tag))

    def _take_baseline(self, data: dict, now: datetime) -> None:
        """Mémorise l'état initial sans notifier."""
        self._baseline_done = True
        if _wan_up(data) is False:
            self._internet.since = now
        for link in data.get("_wans") or []:
            outage = self._links.setdefault(link["id"], _Outage())
            if link["online"] is False:
                outage.since = now
        self._groups_active = {
            g["name"]: tuple(g["active"]) for g in data.get("_wan_groups") or []
        }
        self._default_link = (default_link(data.get("_wans") or []) or {}).get("id")
        self._update_available = _has_update_available(data)
        self._stopped_services = set(
            _services_attributes(data).get("stopped") or []
        )
        self._tunnels_up = {
            t["device"]: t["up"] for t in (_tunnels(data) or [])
        }
        disk = _root_disk_used_percent(data)
        self._disk_alerted = disk is not None and disk >= DISK_ALERT_PCT
        temp = _temp_cpu(data)
        self._temp_alerted = temp is not None and temp >= self.temp_threshold

    def _evaluate(self, data: dict, now: datetime) -> list[tuple[str, str, str]]:
        """Renvoie les notifications à envoyer pour ce cycle."""
        out: list[tuple[str, str, str]] = []
        name = self._firewall_name(data)

        # ---- WAN coupé / rétabli, bascules ----
        if ALERT_WAN in self.enabled:
            out += self._evaluate_wan(data, now, name)

        # ---- Latence élevée ----
        latency = _wan_latency(data)
        if ALERT_LATENCY in self.enabled and latency is not None:
            if latency > self.latency_threshold:
                if self._latency_high_since is None:
                    self._latency_high_since = now
                elapsed = (now - self._latency_high_since).total_seconds()
                if not self._latency_alerted and elapsed >= self.latency_duration:
                    self._latency_alerted = True
                    out.append((
                        f"🟠 {name} : latence élevée",
                        f"Latence WAN à {latency:.1f} ms depuis "
                        f"{_fmt_duration(elapsed)} (seuil "
                        f"{self.latency_threshold} ms).",
                        "latency",
                    ))
            else:
                if self._latency_alerted:
                    out.append((
                        f"🟢 {name} : latence normale",
                        f"Latence WAN revenue à {latency:.1f} ms.",
                        "latency",
                    ))
                self._latency_high_since = None
                self._latency_alerted = False

        # ---- Mise à jour firmware ----
        update = _has_update_available(data)
        if update is not None:
            if (ALERT_FIRMWARE in self.enabled and update
                    and self._update_available is False):
                version = _firmware_latest(data) or "?"
                out.append((
                    f"📦 {name} : mise à jour disponible",
                    f"OPNsense {version} est disponible.",
                    "firmware",
                ))
            self._update_available = update

        # ---- Services arrêtés ----
        if data.get("services") is not None:
            stopped = set(_services_attributes(data).get("stopped") or [])
            if ALERT_SERVICES in self.enabled:
                for svc in sorted(stopped - self._stopped_services):
                    out.append((
                        f"⚙️ {name} : service arrêté",
                        f"Le service « {svc} » s'est arrêté.",
                        f"service-{svc}",
                    ))
                for svc in sorted(self._stopped_services - stopped):
                    out.append((
                        f"🟢 {name} : service relancé",
                        f"Le service « {svc} » tourne de nouveau.",
                        f"service-{svc}",
                    ))
            self._stopped_services = stopped

        # ---- Tunnels VPN ----
        tunnels = _tunnels(data)
        if tunnels is not None:
            for tunnel in tunnels:
                device, up = tunnel["device"], tunnel["up"]
                before = self._tunnels_up.get(device)
                if ALERT_VPN in self.enabled and before is not None and before != up:
                    out.append((
                        f"{'🟢' if up else '🔴'} {name} : tunnel "
                        f"{'rétabli' if up else 'coupé'}",
                        f"{tunnel['kind']} « {tunnel['name']} » est "
                        f"{'de nouveau en ligne' if up else 'hors ligne'}.",
                        f"vpn-{device}",
                    ))
                self._tunnels_up[device] = up

        # ---- Disque ----
        disk = _root_disk_used_percent(data)
        if disk is not None:
            if (ALERT_DISK in self.enabled and not self._disk_alerted
                    and disk >= DISK_ALERT_PCT):
                out.append((
                    f"💽 {name} : disque presque plein",
                    f"La partition racine est utilisée à {disk:.0f} %.",
                    "disk",
                ))
            if disk >= DISK_ALERT_PCT:
                self._disk_alerted = True
            elif disk < DISK_REARM_PCT:
                self._disk_alerted = False

        # ---- Température CPU (hystérésis de TEMP_REARM_DELTA °C) ----
        temp = _temp_cpu(data)
        if temp is not None and ALERT_TEMPERATURE in self.enabled:
            if not self._temp_alerted and temp >= self.temp_threshold:
                self._temp_alerted = True
                out.append((
                    f"🌡️ {name} : température élevée",
                    f"CPU à {temp:.0f} °C (seuil {self.temp_threshold} °C).",
                    "temperature",
                ))
            elif (self._temp_alerted
                    and temp < self.temp_threshold - TEMP_REARM_DELTA):
                self._temp_alerted = False
                out.append((
                    f"🟢 {name} : température normale",
                    f"CPU redescendu à {temp:.0f} °C.",
                    "temperature",
                ))

        return out

    def _evaluate_wan(
        self, data: dict, now: datetime, name: str
    ) -> list[tuple[str, str, str]]:
        """Internet (tous liens), chaque lien en multi-WAN, bascules.

        Quand tous les liens tombent (ou reviennent) dans le même cycle, un
        seul message « Internet coupé / rétabli » remplace ceux des liens.
        """
        links = data.get("_wans") or []
        multi = len(links) >= 2

        # Lien par lien (multi-WAN uniquement : avec un seul WAN, l'alerte
        # « internet » ci-dessous dit déjà tout).
        link_events: list[tuple[str, dict, _Outage]] = []
        for link in links if multi else []:
            if link["online"] is None:
                continue
            outage = self._links.setdefault(link["id"], _Outage())
            event = outage.update(link["online"], now, self.wan_delay)
            if event:
                link_events.append((event, link, outage))

        # Internet : au moins un lien en ligne.
        internet: list[tuple[str, str, str]] = []
        internet_event = None
        wan = _wan_up(data)
        if wan is not None:
            internet_event = self._internet.update(wan, now, self.wan_delay)
        online = [x["name"] for x in links if x["online"]]
        offline = [x["name"] for x in links if x["online"] is False]
        if internet_event == "down":
            internet.append((
                f"🔴 {name} : {'Internet' if multi else 'WAN'} coupé",
                ("Tous les liens WAN sont hors ligne depuis "
                 if multi else "La connexion internet est coupée depuis ")
                + f"{_hhmm(self._internet.since)}.",
                "wan",
            ))
        elif internet_event == "up":
            via = f" par {', '.join(online)}" if multi and online else ""
            still = (f" {', '.join(offline)} toujours hors ligne."
                     if multi and offline else "")
            internet.append((
                f"🟢 {name} : {'Internet' if multi else 'WAN'} rétabli",
                f"Internet est revenu{via} à {_hhmm(now)} après "
                f"{_fmt_duration(self._internet.last_elapsed)} de coupure "
                f"(depuis {_hhmm(self._internet.last_since)}).{still}",
                "wan",
            ))

        out: list[tuple[str, str, str]] = []
        for event, link, outage in link_events:
            if internet_event == event:
                continue  # couvert par « Internet coupé / rétabli »
            if event == "down":
                relay = (f" Internet passe par {', '.join(online)}."
                         if online else "")
                detail = f" ({link['status']})" if link.get("status") else ""
                out.append((
                    f"🔴 {name} : {link['name']} coupé",
                    f"Le lien {link['name']} est hors ligne depuis "
                    f"{_hhmm(outage.since)}{detail}.{relay}",
                    f"wan-{link['id']}",
                ))
            else:
                out.append((
                    f"🟢 {name} : {link['name']} rétabli",
                    f"Le lien {link['name']} est revenu à {_hhmm(now)} après "
                    f"{_fmt_duration(outage.last_elapsed)} de coupure.",
                    f"wan-{link['id']}",
                ))
        out += internet

        # Bascules : groupes de passerelles s'il y en a, sinon route par
        # défaut. Une reprise après coupure totale n'est pas une bascule.
        groups = data.get("_wan_groups")
        if groups:
            for group in groups:
                active = tuple(group["active"])
                before = self._groups_active.get(group["name"])
                self._groups_active[group["name"]] = active
                if not before or not active or before == active:
                    continue
                out.append((
                    f"🔀 {name} : bascule {group['name']}",
                    f"Le groupe {group['name']} passe par {' + '.join(active)}"
                    f" (avant : {' + '.join(before)}).",
                    f"failover-{group['name']}",
                ))
        elif multi:
            main = default_link(links)
            current = main["id"] if main and main["online"] else None
            before = self._default_link
            if current:
                self._default_link = current
            if before and current and before != current:
                previous = next(
                    (x["name"] for x in links if x["id"] == before), before
                )
                out.append((
                    f"🔀 {name} : bascule sur {main['name']}",
                    f"La route par défaut passe par {main['name']} "
                    f"(avant : {previous}).",
                    "failover",
                ))
        return out

    def _firewall_name(self, data: dict) -> str:
        """Nom court du pare-feu (hostname sans domaine)."""
        hostname = _get(data, "system_information", "name")
        if isinstance(hostname, str) and hostname:
            return hostname.split(".")[0]
        return "OPNsense"

    async def _send(self, title: str, message: str, tag: str) -> None:
        """Envoie la notification (persistante + services notify)."""
        if self.persistent:
            try:
                await self.hass.services.async_call(
                    "persistent_notification", "create",
                    {"notification_id": f"opnsense_{self.entry.entry_id}_{tag}",
                     "title": title, "message": message},
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Notification persistante impossible: %s", err)
        for target in self.targets:
            if not self.hass.services.has_service("notify", target):
                _LOGGER.debug("Service notify.%s introuvable", target)
                continue
            payload: dict[str, Any] = {"title": title, "message": message}
            if target.startswith("mobile_app_"):
                # Push iOS/Android : passe le mode Concentration et remplace
                # la notif précédente du même sujet (coupé -> rétabli).
                payload["data"] = {
                    "tag": f"opnsense-{tag}", "group": "opnsense",
                    "push": {"interruption-level": "time-sensitive"},
                }
            try:
                await self.hass.services.async_call("notify", target, payload)
            except Exception as err:  # noqa: BLE001 - WAN coupé, etc.
                _LOGGER.debug("Notification %s impossible: %s", target, err)
