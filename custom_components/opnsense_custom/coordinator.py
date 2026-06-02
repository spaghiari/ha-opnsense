"""Coordinator de polling pour OPNsense custom."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .api import OPNsenseApiClient, OPNsenseApiError
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class OPNsenseDataCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator qui appelle async_get_all() périodiquement.

    Toutes les entités (sensors, binary_sensors, update) lisent
    self.data pour leurs valeurs. Évite que chaque entité fasse
    son propre appel HTTP.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        client: OPNsenseApiClient,
        scan_interval: int,
        entry: ConfigEntry,
    ) -> None:
        """Initialise le coordinator avec un intervalle de polling."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(seconds=scan_interval),
        )
        self.client = client
        self.entry = entry

    async def _async_update_data(self) -> dict[str, Any]:
        """Appelé automatiquement toutes les `scan_interval` secondes."""
        try:
            data = await self.client.async_get_all()
        except OPNsenseApiError as err:
            raise UpdateFailed(f"Erreur API OPNsense: {err}") from err

        # On vérifie qu'on a au moins les infos système, sinon ça ne sert à rien
        if data.get("system_information") is None:
            raise UpdateFailed(
                "Impossible de récupérer system_information - "
                "vérifier les privilèges API"
            )

        return data
