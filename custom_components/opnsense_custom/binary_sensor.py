"""Binary sensors OPNsense custom."""
from __future__ import annotations

from typing import Any

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
from .wans import find_link, internet_up, links_with_entities


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
    """True si internet est joignable par au moins un lien WAN.

    Un lien est coupé si son interface est down ou si sa passerelle est
    hors ligne / à 100 % de pertes (panne opérateur). Sans liens détectés,
    repli sur l'état de l'interface WAN résolue.
    """
    links = data.get("_wans")
    if links:
        return internet_up(links)
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

    # Le WAN est suivi au rythme du polling rapide ; la MAJ au rythme lent.
    # Les infos d'appareil viennent toujours du coordinator principal
    # (version du firmware), quel que soit le coordinator écouté.
    entities = [
        OPNsenseUpdateAvailableBinary(coordinator, entry),
        OPNsenseWanUpBinary(coordinator.fast, entry, coordinator.data),
    ]
    async_add_entities(entities)

    # Multi-WAN : un « <lien> connecté » par lien, ajouté dès qu'il apparaît.
    known: set[str] = set()

    def _sync_links() -> None:
        new = []
        for link in links_with_entities(coordinator.fast.data or {}):
            if link["id"] not in known:
                known.add(link["id"])
                new.append(OPNsenseLinkUpBinary(
                    coordinator.fast, entry, link, coordinator.data
                ))
        if new:
            async_add_entities(new)

    _sync_links()
    entry.async_on_unload(coordinator.fast.async_add_listener(_sync_links))


class _OPNsenseBinaryBase(CoordinatorEntity, BinarySensorEntity):
    """Base partagée pour les binary sensors."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: Any,
        entry: ConfigEntry,
        description: BinarySensorEntityDescription,
        device_data: dict | None = None,
    ) -> None:
        """Initialise."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = build_device_info(
            entry, device_data if device_data is not None else coordinator.data
        )


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
        coordinator: Any,
        entry: ConfigEntry,
        device_data: dict | None = None,
    ) -> None:
        """Initialise (coordinator = polling rapide)."""
        description = BinarySensorEntityDescription(
            key="wan_connected",
            translation_key="wan_connected",
            device_class=BinarySensorDeviceClass.CONNECTIVITY,
        )
        super().__init__(coordinator, entry, description, device_data)

    @property
    def is_on(self) -> bool | None:
        """État."""
        if self.coordinator.data is None:
            return None
        return _wan_up(self.coordinator.data)


class OPNsenseLinkUpBinary(_OPNsenseBinaryBase):
    """True si un lien WAN donné est en ligne (multi-WAN)."""

    def __init__(
        self,
        coordinator: Any,
        entry: ConfigEntry,
        link: dict,
        device_data: dict | None = None,
    ) -> None:
        """Initialise (coordinator = polling rapide)."""
        self._link_id = link["id"]
        description = BinarySensorEntityDescription(
            key=f"wan_{link['id']}_connected",
            translation_key="link_connected",
            device_class=BinarySensorDeviceClass.CONNECTIVITY,
        )
        super().__init__(coordinator, entry, description, device_data)
        self._attr_translation_placeholders = {"wan": link["name"]}

    def _link(self) -> dict | None:
        return find_link(self.coordinator.data or {}, self._link_id)

    @property
    def available(self) -> bool:
        """Indisponible si le lien a disparu (passerelle supprimée, exclue)."""
        return super().available and self._link() is not None

    @property
    def is_on(self) -> bool | None:
        """État."""
        return (self._link() or {}).get("online")

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Passerelle, statut dpinger, route par défaut."""
        link = self._link()
        if link is None:
            return None
        return {key: link.get(key) for key in (
            "device", "gateway", "gateways", "status", "default",
            "delay_ms", "loss_pct",
        )}
