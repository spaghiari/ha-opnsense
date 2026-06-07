"""Binary sensors OPNsense custom."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import OPNsenseDataCoordinator, build_device_info, find_wan_row


def _has_update_available(data: dict) -> bool | None:
    """Renvoie True si une mise à jour est disponible.

    OPNsense indique cela dans firmware_status :
    - "status" peut valoir "update", "ok", "none", etc.
    - "status_upgrade_action" ou "needs_reboot" sont d'autres indices
    """
    fw = data.get("firmware_status")
    if not isinstance(fw, dict):
        return None
    status = fw.get("status")
    if status in ("update", "upgrade"):
        return True
    if status in ("ok", "none", "uptodate", "up_to_date"):
        return False
    # On regarde aussi le nombre de paquets à mettre à jour
    upgrade_packages = fw.get("upgrade_packages")
    if isinstance(upgrade_packages, list):
        return len(upgrade_packages) > 0
    return None


def _wan_up(data: dict) -> bool | None:
    """True si l'interface WAN (résolue par le coordinator) est up."""
    wan = find_wan_row(data)
    if wan is None:
        return None
    return wan.get("status") == "up"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Crée les binary sensors."""
    coordinator: OPNsenseDataCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = [
        OPNsenseUpdateAvailableBinary(coordinator, entry),
        OPNsenseWanUpBinary(coordinator, entry),
    ]
    async_add_entities(entities)


class _OPNsenseBinaryBase(
    CoordinatorEntity[OPNsenseDataCoordinator], BinarySensorEntity
):
    """Base partagée pour les binary sensors."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OPNsenseDataCoordinator,
        entry: ConfigEntry,
        description: BinarySensorEntityDescription,
    ) -> None:
        """Initialise."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = build_device_info(entry, coordinator.data)


class OPNsenseUpdateAvailableBinary(_OPNsenseBinaryBase):
    """True si une mise à jour OPNsense est disponible."""

    def __init__(
        self,
        coordinator: OPNsenseDataCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialise."""
        description = BinarySensorEntityDescription(
            key="update_available",
            translation_key="update_available",
            device_class=BinarySensorDeviceClass.UPDATE,
            icon="mdi:package-up",
        )
        super().__init__(coordinator, entry, description)

    @property
    def is_on(self) -> bool | None:
        """État."""
        if self.coordinator.data is None:
            return None
        return _has_update_available(self.coordinator.data)


class OPNsenseWanUpBinary(_OPNsenseBinaryBase):
    """True si le WAN est up."""

    def __init__(
        self,
        coordinator: OPNsenseDataCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialise."""
        description = BinarySensorEntityDescription(
            key="wan_connected",
            translation_key="wan_connected",
            device_class=BinarySensorDeviceClass.CONNECTIVITY,
        )
        super().__init__(coordinator, entry, description)

    @property
    def is_on(self) -> bool | None:
        """État."""
        if self.coordinator.data is None:
            return None
        return _wan_up(self.coordinator.data)
