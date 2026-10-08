"""Flux temps réel OPNsense : débit WAN et CPU poussés par le pare-feu.

OPNsense expose, pour son propre dashboard, deux flux continus (SSE) :
  * /api/diagnostics/traffic/stream/1 : chaque seconde, pour chaque
    interface, les octets échangés depuis l'événement précédent + un
    horodatage précis ;
  * /api/diagnostics/cpu_usage/stream : chaque seconde, le CPU en %.

Ce module garde ces deux connexions ouvertes, cumule octets et durées entre
deux publications, puis publie dans Home Assistant toutes les
`publish_interval` secondes un débit moyen exact (octets x 8 / temps écoulé)
et le dernier CPU. On évite ainsi d'écrire un état par seconde dans la base
tout en gardant un affichage quasi instantané.

Robustesse :
  * reconnexion automatique avec attente croissante (5 s -> 60 s) ;
  * 401 : clé invalide, le flux s'arrête (le polling déclenche la ré-auth) ;
  * 403 / 404 : privilège ou endpoint absent, nouvel essai toutes les 10 min ;
  * si un flux se tait, `fresh_data()` ne renvoie plus ses valeurs et les
    capteurs se rabattent sur le débit calculé par le polling rapide.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from time import monotonic
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .api import (
    OPNsenseApiClient,
    OPNsenseApiError,
    OPNsenseAuthError,
    OPNsenseForbiddenError,
)
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

RETRY_MIN = 5          # s, première attente avant reconnexion
RETRY_MAX = 60         # s, attente maximale
RETRY_FORBIDDEN = 600  # s, privilège / endpoint absent


class OPNsenseLiveCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator "push" alimenté par les flux temps réel d'OPNsense."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: OPNsenseApiClient,
        entry: ConfigEntry,
        publish_interval: int,
        wan_identifier: Any,
    ) -> None:
        """`wan_identifier` : callable renvoyant l'identifiant de config du WAN."""
        super().__init__(
            hass, _LOGGER, name=f"{DOMAIN}_{entry.entry_id}_live",
            update_interval=None,
        )
        self.client = client
        self.publish_interval = publish_interval
        self._wan_identifier = wan_identifier
        self._tasks: list[asyncio.Task] = []
        # Cumul depuis la dernière publication
        self._acc_rx = 0
        self._acc_tx = 0
        self._acc_time = 0.0
        self._prev_time: float | None = None
        self._cpu: dict[str, Any] | None = None
        self._last_publish = monotonic()
        # Diagnostic / fraîcheur
        self.status: dict[str, dict[str, Any]] = {
            "traffic": {"state": "starting", "events": 0, "last": None,
                        "error": None},
            "cpu": {"state": "starting", "events": 0, "last": None,
                    "error": None},
        }
        self.data = {}

    # ------------------------------------------------------------------
    def start(self) -> None:
        """Lance les deux flux en tâches de fond."""
        self._tasks = [
            self.hass.async_create_background_task(
                self._run("traffic", "traffic_stream", {"interval": "1"},
                          self._on_traffic),
                f"{self.name}_traffic",
            ),
            self.hass.async_create_background_task(
                self._run("cpu", "cpu_stream", {}, self._on_cpu),
                f"{self.name}_cpu",
            ),
        ]

    async def async_stop(self) -> None:
        """Ferme proprement les flux (déchargement de l'entry)."""
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with suppress(asyncio.CancelledError):
                await task
        self._tasks = []

    def fresh_data(self) -> dict[str, Any]:
        """Valeurs publiées encore fraîches (sinon les capteurs se rabattent)."""
        max_age = max(3 * self.publish_interval, 10)
        now = monotonic()
        fresh: dict[str, Any] = {}
        for key, stream in (("wan", "traffic"), ("cpu", "cpu")):
            last = self.status[stream]["last"]
            if last is not None and now - last <= max_age and key in self.data:
                fresh[key] = self.data[key]
        return fresh

    # ------------------------------------------------------------------
    async def _run(self, name: str, endpoint: str, params: dict[str, str],
                   handler: Any) -> None:
        """Boucle de connexion d'un flux, avec reconnexion."""
        status = self.status[name]
        delay = RETRY_MIN
        forbidden_logged = False
        while True:
            try:
                if status["state"] != "forbidden":
                    status.update(state="connecting", error=None)
                if name == "traffic":
                    # Nouveau flux : son premier événement n'a pas de durée
                    # de référence (ne pas le comparer à l'ancienne connexion).
                    self._prev_time = None
                async for event in self.client.async_stream(endpoint, **params):
                    if status["state"] != "streaming":
                        status["state"] = "streaming"
                        _LOGGER.info("Flux temps réel '%s' connecté", name)
                    delay = RETRY_MIN
                    status["events"] += 1
                    status["last"] = monotonic()
                    handler(event)
                    self._maybe_publish()
            except asyncio.CancelledError:
                raise
            except OPNsenseAuthError as err:
                status.update(state="auth_error", error=str(err))
                _LOGGER.warning("Flux '%s' : authentification refusée", name)
                return
            except OPNsenseForbiddenError as err:
                status.update(state="forbidden", error=str(err))
                if not forbidden_logged:
                    forbidden_logged = True
                    _LOGGER.warning(
                        "Flux temps réel '%s' non autorisé (%s) : repli sur le "
                        "polling rapide, nouvel essai toutes les 10 min "
                        "(message affiché une seule fois)", name, err,
                    )
                await asyncio.sleep(RETRY_FORBIDDEN)
                continue
            except OPNsenseApiError as err:
                was_streaming = status["state"] == "streaming"
                status.update(state="reconnecting", error=str(err))
                if was_streaming:
                    _LOGGER.info("Flux '%s' interrompu (%s), reconnexion", name, err)
                else:
                    _LOGGER.debug("Flux '%s' indisponible: %s", name, err)
            except Exception as err:  # noqa: BLE001 - ne jamais tuer la boucle
                status.update(state="reconnecting", error=repr(err))
                _LOGGER.debug("Flux '%s' : erreur inattendue %r", name, err)
            await asyncio.sleep(delay)
            delay = min(delay * 2, RETRY_MAX)

    def _on_traffic(self, event: dict[str, Any]) -> None:
        """Cumule les octets WAN de l'événement (déjà en différence)."""
        stamp = event.get("time")
        prev, self._prev_time = self._prev_time, stamp
        if not isinstance(stamp, int | float) or prev is None:
            return  # premier événement : durée inconnue
        elapsed = stamp - prev
        iface = (event.get("interfaces") or {}).get(self._wan_identifier())
        if elapsed <= 0 or elapsed > 30 or not isinstance(iface, dict):
            return
        try:
            self._acc_rx += max(int(iface.get("inbytes", 0)), 0)
            self._acc_tx += max(int(iface.get("outbytes", 0)), 0)
        except (TypeError, ValueError):
            return
        self._acc_time += elapsed

    def _on_cpu(self, event: dict[str, Any]) -> None:
        """Mémorise la dernière mesure CPU (en %)."""
        if "total" in event:
            self._cpu = event

    def _maybe_publish(self) -> None:
        """Publie un débit moyen toutes les `publish_interval` secondes."""
        if monotonic() - self._last_publish < self.publish_interval:
            return
        self._last_publish = monotonic()
        data = dict(self.data or {})
        if self._acc_time > 0:
            data["wan"] = {
                "in_bps": int(self._acc_rx * 8 / self._acc_time),
                "out_bps": int(self._acc_tx * 8 / self._acc_time),
            }
            self._acc_rx = self._acc_tx = 0
            self._acc_time = 0.0
        if self._cpu is not None:
            data["cpu"] = self._cpu
        self.async_set_updated_data(data)

    def diagnostics(self) -> dict[str, Any]:
        """État des flux pour le diagnostic téléchargeable."""
        now = monotonic()
        return {
            name: {
                "state": s["state"], "events": s["events"], "error": s["error"],
                "seconds_since_last_event": (
                    round(now - s["last"], 1) if s["last"] is not None else None
                ),
            }
            for name, s in self.status.items()
        } | {"publish_interval": self.publish_interval,
             "last_published": self.data}
