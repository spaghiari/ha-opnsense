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
from homeassistant.helpers.storage import Store
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
    DEFAULT_ALERTS,
    DEFAULT_LATENCY_DURATION,
    DEFAULT_LATENCY_THRESHOLD,
    DEFAULT_NOTIFY_PERSISTENT,
    DEFAULT_TEMP_THRESHOLD,
    DEFAULT_WAN_DOWN_DELAY,
    DISK_ALERT_PCT,
    DISK_REARM_PCT,
    DOMAIN,
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

# Persistance de l'état des alertes (survit au reload / redémarrage de HA).
STORE_VERSION = 1
STORE_SAVE_DELAY = 5  # secondes : regroupe les écritures en rafale
STORE_REFRESH = 3600  # secondes : rafraîchit `saved_at` même sans transition
STORE_MAX_AGE = 7 * 86400  # secondes : au-delà, l'état est ignoré


def _store_key(entry_id: str) -> str:
    """Clé du fichier Store d'une entry."""
    return f"{DOMAIN}.alerts.{entry_id}"


def _iso(moment: datetime | None) -> str | None:
    """Horodatage -> ISO 8601 (None conservé)."""
    return moment.isoformat() if moment else None


def _parse(value: Any) -> datetime | None:
    """ISO 8601 -> datetime UTC ; None si absent, invalide ou dans le futur."""
    if not isinstance(value, str):
        return None
    parsed = dt_util.parse_datetime(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_util.UTC)
    if parsed > dt_util.utcnow():
        return None
    return parsed


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
        # Coupure à cheval sur un redémarrage de HA : dernier relevé hors
        # ligne avant l'arrêt (None sinon) ; sert à formuler « rétabli » sans
        # inventer d'heure de retour ni de durée exacte.
        self.restored_seen: datetime | None = None
        self.last_seen: datetime | None = None

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
        self.last_seen, self.restored_seen = self.restored_seen, None
        notify = self.alerted or self.last_elapsed >= delay
        self.since, self.alerted = None, False
        return "up" if notify else None

    def as_dict(self) -> dict[str, Any]:
        """État minimal à persister."""
        return {"since": _iso(self.since), "alerted": self.alerted}

    @classmethod
    def from_dict(cls, raw: Any, saved_at: datetime) -> _Outage:
        """Reconstruit une coupure depuis l'état persisté (tolérant).

        Seule une coupure déjà notifiée est reprise : sinon le délai de grâce
        repart de zéro (référence prise à la première évaluation).
        """
        outage = cls()
        if isinstance(raw, dict) and raw.get("alerted") is True:
            since = _parse(raw.get("since"))
            if since is not None and since <= saved_at:
                outage.since, outage.alerted = since, True
                outage.restored_seen = saved_at
        return outage


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
        # Clé absente (options jamais enregistrées) -> alertes par défaut,
        # comme le formulaire ; liste vide explicite -> tout désactivé.
        if CONF_ALERTS in options:
            self.enabled: set[str] = set(options.get(CONF_ALERTS) or [])
        else:
            self.enabled = set(DEFAULT_ALERTS)
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
        # Persistance : état restauré (None = rien) et dernier état écrit.
        self._store: Store[dict[str, Any]] = Store(
            hass, STORE_VERSION, _store_key(entry.entry_id)
        )
        self._restored: dict[str, Any] | None = None
        # Clés effectivement reprises de l'état restauré (le reste : ligne
        # de base prise depuis les données).
        self._restored_keys: set[str] = set()
        self._last_saved: dict[str, Any] | None = None
        self._last_save_at: datetime | None = None

    async def async_load(self) -> None:
        """Charge l'état persisté ; à appeler avant la première évaluation.

        Si aucune alerte n'est active, le fichier est supprimé : un état
        ancien ne doit pas resservir si on recoche les alertes plus tard.
        """
        if not self.active:
            await self._remove()
            return
        try:
            raw = await self._store.async_load()
        except Exception as err:  # noqa: BLE001 - fichier corrompu : repart à zéro
            _LOGGER.debug("Lecture de l'état des alertes impossible: %s", err)
            return
        if isinstance(raw, dict):
            self._restored = raw

    async def async_flush(self) -> None:
        """Écrit l'état courant tout de suite (déchargement de l'entry).

        `async_save` annule l'écriture différée en attente : un reload rapide
        relit l'état à jour, et aucun timer ne survit à l'instance.
        """
        if not self.active or not self._baseline_done:
            return  # rien à écrire (le fichier existant reste intact)
        try:
            await self._store.async_save(self._data_to_save())
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Écriture de l'état des alertes impossible: %s", err)

    async def _remove(self) -> None:
        """Supprime le fichier (et annule toute écriture différée)."""
        try:
            await self._store.async_remove()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Suppression de l'état des alertes impossible: %s", err)

    @staticmethod
    async def async_remove_store(hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Supprime le fichier Store à la suppression de l'entry.

        Appelé après le déchargement (donc après `async_flush`) : plus aucune
        écriture différée ne peut recréer le fichier.
        """
        try:
            store: Store[dict[str, Any]] = Store(
                hass, STORE_VERSION, _store_key(entry.entry_id)
            )
            await store.async_remove()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Suppression de l'état des alertes impossible: %s", err)

    def _snapshot(self) -> dict[str, Any]:
        """État minimal à persister (JSON-sérialisable, sans horodatage)."""
        return {
            "internet": self._internet.as_dict(),
            # Seules les coupures notifiées sont utiles après un redémarrage.
            "links": {k: v.as_dict() for k, v in self._links.items() if v.alerted},
            "latency_since": _iso(self._latency_high_since),
            "latency_alerted": self._latency_alerted,
            "update_available": self._update_available,
            "stopped_services": sorted(self._stopped_services),
            "tunnels_up": dict(self._tunnels_up),
            "disk_alerted": self._disk_alerted,
            "temp_alerted": self._temp_alerted,
        }

    def _data_to_save(self) -> dict[str, Any]:
        """Snapshot + `saved_at` (dernier relevé), calculé à l'écriture."""
        return {**self._snapshot(), "saved_at": _iso(dt_util.utcnow())}

    def _restore(self, now: datetime) -> None:
        """Applique l'état persisté avant la ligne de base (tolérant).

        Reprend clé par clé ce qui est valide ET concerne une alerte
        actuellement activée ; le reste est recalculé depuis les données.
        Un état trop ancien ou sans `saved_at` valide est ignoré.
        """
        raw, self._restored = self._restored, None
        self._restored_keys = set()
        if not raw:
            return
        saved_at = _parse(raw.get("saved_at"))
        if saved_at is None or (now - saved_at).total_seconds() > STORE_MAX_AGE:
            return
        keys = self._restored_keys
        if ALERT_WAN in self.enabled:
            internet = _Outage.from_dict(raw.get("internet"), saved_at)
            links = raw.get("links")
            restored_links = {
                str(k): _Outage.from_dict(v, saved_at)
                for k, v in (links.items() if isinstance(links, dict) else [])
            }
            self._internet = internet
            self._links = {k: v for k, v in restored_links.items() if v.alerted}
            if internet.alerted or self._links:
                keys.add("wan")
        if ALERT_LATENCY in self.enabled and raw.get("latency_alerted") is True:
            since = _parse(raw.get("latency_since"))
            if since is not None:
                self._latency_high_since, self._latency_alerted = since, True
                keys.add("latency")
        update = raw.get("update_available")
        if ALERT_FIRMWARE in self.enabled and isinstance(update, bool):
            self._update_available = update
            keys.add("update")
        stopped = raw.get("stopped_services")
        if (ALERT_SERVICES in self.enabled and isinstance(stopped, list)
                and all(isinstance(s, str) for s in stopped)):
            self._stopped_services = set(stopped)
            keys.add("services")
        tunnels = raw.get("tunnels_up")
        if (ALERT_VPN in self.enabled and isinstance(tunnels, dict)
                and all(isinstance(v, bool) for v in tunnels.values())):
            self._tunnels_up = {str(k): v for k, v in tunnels.items()}
            keys.add("tunnels")
        if ALERT_DISK in self.enabled and isinstance(raw.get("disk_alerted"), bool):
            self._disk_alerted = raw["disk_alerted"]
            keys.add("disk")
        if (ALERT_TEMPERATURE in self.enabled
                and isinstance(raw.get("temp_alerted"), bool)):
            self._temp_alerted = raw["temp_alerted"]
            keys.add("temp")

    @callback
    def _save_if_changed(self, now: datetime) -> None:
        """Planifie une écriture différée si l'état a changé (ou est périmé)."""
        snapshot = self._snapshot()
        fresh = (
            self._last_save_at is not None
            and (now - self._last_save_at).total_seconds() < STORE_REFRESH
        )
        if snapshot == self._last_saved and fresh:
            return
        self._last_saved, self._last_save_at = snapshot, now
        self._store.async_delay_save(self._data_to_save, STORE_SAVE_DELAY)

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
            if not self._restored_keys:
                self._save_if_changed(now)
                return
            # État restauré : évalue tout de suite pour notifier ce qui a
            # changé pendant l'arrêt (ex. « rétabli » d'une coupure notifiée).
        for title, message, tag in self._evaluate(data, now):
            self.entry.async_create_task(
                self.hass,
                self._send(title, message, tag),
                name=f"{DOMAIN} alerte {tag}",
            )
        self._save_if_changed(now)

    def _take_baseline(self, data: dict, now: datetime) -> None:
        """Mémorise l'état initial sans notifier (en reprenant l'état restauré)."""
        self._baseline_done = True
        self._restore(now)
        keys = self._restored_keys
        # Coupure en cours : début à now sauf coupure notifiée restaurée. Si
        # le lien n'est plus hors ligne, une coupure restaurée sera close
        # (« rétabli ») par la première évaluation ; si elle continue, le
        # message de retour sera un message normal.
        wan = _wan_up(data)
        if wan is False and self._internet.since is None:
            self._internet.since = now
        if wan is not True:
            self._internet.restored_seen = None
        for link in data.get("_wans") or []:
            outage = self._links.setdefault(link["id"], _Outage())
            if link["online"] is False and outage.since is None:
                outage.since = now
            if link["online"] is not True:
                outage.restored_seen = None
        self._groups_active = {
            g["name"]: tuple(g["active"]) for g in data.get("_wan_groups") or []
        }
        self._default_link = (default_link(data.get("_wans") or []) or {}).get("id")
        # Repli par clé : ce qui n'a pas été restauré est pris dans les données.
        if "update" not in keys:
            self._update_available = _has_update_available(data)
        if "services" not in keys:
            self._stopped_services = set(
                _services_attributes(data).get("stopped") or []
            )
        if "tunnels" not in keys:
            self._tunnels_up = {
                t["device"]: t["up"] for t in (_tunnels(data) or [])
            }
        if "disk" not in keys:
            disk = _root_disk_used_percent(data)
            self._disk_alerted = disk is not None and disk >= DISK_ALERT_PCT
        if "temp" not in keys:
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
            if self._internet.last_seen is not None:
                # Coupure à cheval sur un redémarrage de HA : durée inconnue.
                known = (self._internet.last_seen
                         - self._internet.last_since).total_seconds()
                detail = (
                    f"Internet est rétabli{via} (constaté au redémarrage de "
                    f"l'intégration à {_hhmm(now)}) : coupure d'au moins "
                    f"{_fmt_duration(known)}, dernier relevé hors ligne à "
                    f"{_hhmm(self._internet.last_seen)}.{still}"
                )
            else:
                detail = (
                    f"Internet est revenu{via} à {_hhmm(now)} après "
                    f"{_fmt_duration(self._internet.last_elapsed)} de coupure "
                    f"(depuis {_hhmm(self._internet.last_since)}).{still}"
                )
            internet.append((
                f"🟢 {name} : {'Internet' if multi else 'WAN'} rétabli",
                detail,
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
                if outage.last_seen is not None:
                    known = (outage.last_seen - outage.last_since).total_seconds()
                    detail = (
                        f"Le lien {link['name']} est rétabli (constaté au "
                        f"redémarrage de l'intégration à {_hhmm(now)}) : "
                        f"coupure d'au "
                        f"moins {_fmt_duration(known)}, dernier relevé hors "
                        f"ligne à {_hhmm(outage.last_seen)}."
                    )
                else:
                    detail = (
                        f"Le lien {link['name']} est revenu à {_hhmm(now)} "
                        f"après {_fmt_duration(outage.last_elapsed)} de coupure."
                    )
                out.append((
                    f"🟢 {name} : {link['name']} rétabli",
                    detail,
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
