"""Entité Update OPNsense custom - cœur de la fonctionnalité MAJ."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityDescription,
    UpdateEntityFeature,
)
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
    """Crée l'entité Update."""
    coordinator: OPNsenseDataCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([OPNsenseUpdate(coordinator, entry)])


class OPNsenseUpdate(CoordinatorEntity[OPNsenseDataCoordinator], UpdateEntity):
    """Entité Update : compare version installée vs disponible.

    Comportement attendu dans Home Assistant :
    - Si une MAJ est dispo, affichée comme "Update available" + bouton Install
    - Le bouton "Install" déclenche async_install (POST /firmware/update)
    - Le polling normal détecte ensuite la nouvelle version
    """

    _attr_has_entity_name = True
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_supported_features = (
        UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS
    )

    def __init__(
        self,
        coordinator: OPNsenseDataCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialise."""
        super().__init__(coordinator)
        self.entity_description = UpdateEntityDescription(
            key="firmware",
            translation_key="firmware",
        )
        self._attr_unique_id = f"{entry.entry_id}_firmware_update"
        self._attr_title = "OPNsense"
        self._installing = False
        self._attr_device_info = build_device_info(entry, coordinator.data)

    def _fw(self) -> dict[str, Any] | None:
        """Renvoie le sous-dict firmware_status si dispo."""
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("firmware_status")

    def _product(self) -> dict[str, Any] | None:
        """Sous-dict product, l'emplacement varie selon les versions OPNsense."""
        fw = self._fw()
        if not fw:
            return None
        prod = fw.get("product")
        return prod if isinstance(prod, dict) else fw

    @property
    def installed_version(self) -> str | None:
        """Version actuellement installée."""
        prod = self._product()
        if not prod:
            return None
        return prod.get("product_version") or prod.get("product_series")

    @property
    def latest_version(self) -> str | None:
        """Dernière version dispo (ou la version installée si à jour).

        On renvoie installed_version si pas de MAJ détectée, car HA
        considère "à jour" quand installed == latest.
        """
        prod = self._product()
        installed = self.installed_version
        if not prod:
            return installed
        latest = prod.get("product_latest")
        if latest and latest != "0":
            return latest
        # Si pas de version "latest" explicite, on regarde le status global
        fw = self._fw()
        if fw and fw.get("status") in ("update", "upgrade"):
            # Une MAJ existe mais on ne connaît pas la version exacte.
            # On renvoie un marqueur générique pour signaler la dispo.
            return "available"
        return installed

    @property
    def in_progress(self) -> bool:
        """True pendant l'installation."""
        return self._installing

    @property
    def release_url(self) -> str | None:
        """Lien vers les release notes."""
        return "https://docs.opnsense.org/releases.html"

    async def async_install(
        self,
        version: str | None = None,
        backup: bool = False,
        **kwargs: Any,
    ) -> None:
        """Déclenche la MAJ côté OPNsense.

        Note : OPNsense ne supporte pas la sélection d'une version cible
        précise via cet endpoint - il installe la "latest" disponible.
        Le paramètre backup est ignoré (à gérer manuellement côté firewall).
        """
        _LOGGER.warning(
            "Déclenchement de la mise à jour OPNsense via HA - "
            "le firewall peut redémarrer pendant l'opération"
        )
        self._installing = True
        self.async_write_ha_state()

        try:
            await self.coordinator.client.async_run_update()
        except OPNsenseApiError as err:
            _LOGGER.error("Erreur lors de l'installation : %s", err)
            self._installing = False
            self.async_write_ha_state()
            raise

        # On laisse OPNsense faire son travail. Le coordinator
        # détectera la fin de la MAJ via le status au prochain polling.
        # On ne remet pas _installing à False ici : le polling reverra
        # un status "ok" et la version mise à jour, ce qui clôt l'UI.
        await self.coordinator.async_request_refresh()

    @property
    def available(self) -> bool:
        """Disponible si le coordinator a des données firmware."""
        return super().available and self._fw() is not None

    def _handle_coordinator_update(self) -> None:
        """Reset le flag installing quand la version installée change.

        NB : le bon hook de CoordinatorEntity est `_handle_coordinator_update`
        (et non `_coordinator_updated`, qui n'est jamais appelé). Sans ça, le
        flag `_installing` ne redescendait jamais et l'entité restait bloquée
        en "installation en cours".
        """
        # Si HA voit une nouvelle version installée, l'installation est finie
        if self._installing and self.installed_version == self.latest_version:
            self._installing = False
        super()._handle_coordinator_update()
