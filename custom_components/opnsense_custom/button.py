"""Boutons OPNsense custom."""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import OPNsenseApiError
from .const import DOMAIN
from .coordinator import OPNsenseDataCoordinator, build_device_info

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Crée les boutons."""
    coordinator: OPNsenseDataCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([OPNsenseCheckUpdatesButton(coordinator, entry)])


class OPNsenseCheckUpdatesButton(
    CoordinatorEntity[OPNsenseDataCoordinator], ButtonEntity
):
    """Bouton 'Vérifier les mises à jour'.

    Déclenche un POST /api/core/firmware/check côté OPNsense,
    puis force un refresh du coordinator pour récupérer le résultat.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OPNsenseDataCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialise."""
        super().__init__(coordinator)
        self.entity_description = ButtonEntityDescription(
            key="check_updates",
            translation_key="check_updates",
            icon="mdi:cloud-search-outline",
        )
        self._attr_unique_id = f"{entry.entry_id}_check_updates"
        self._attr_device_info = build_device_info(entry, coordinator.data)

    async def async_press(self) -> None:
        """Appelle l'API pour lancer le check, puis refresh."""
        try:
            await self.coordinator.client.async_check_for_updates()
        except OPNsenseApiError as err:
            _LOGGER.error("Échec du check de mise à jour : %s", err)
            return
        # Le check OPNsense est asynchrone côté firewall, mais le polling
        # suivant verra le nouveau status. On force un refresh immédiat.
        await self.coordinator.async_request_refresh()
