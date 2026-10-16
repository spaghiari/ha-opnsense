"""Entité Update OPNsense custom - cœur de la fonctionnalité MAJ."""
from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityDescription,
    UpdateEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import OPNsenseApiError, OPNsenseAuthError, OPNsenseForbiddenError
from .const import DOMAIN
from .coordinator import OPNsenseDataCoordinator, build_device_info

_LOGGER = logging.getLogger(__name__)

# Suivi de l'installation via /firmware/upgradestatus
# (> HTTP_TIMEOUT : pas de chevauchement de deux sondages)
UPGRADE_POLL_INTERVAL = timedelta(seconds=30)
# Garde-fou : au-delà, on considère l'installation terminée (ou perdue)
UPGRADE_MAX_DURATION = 45 * 60
# Délai pendant lequel un statut terminal est ignoré tant qu'aucune activité
# (running ou API injoignable) n'a été vue : upgradestatus peut encore
# renvoyer le résultat de l'exécution précédente juste après le lancement.
UPGRADE_STALE_GRACE = 90


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
    - Le bouton "Install" déclenche async_install (POST /firmware/update,
      ou /firmware/upgrade si le statut firmware vaut "upgrade")
    - La progression est suivie par sondage de /firmware/upgradestatus
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
        # Suivi de l'installation (sondage upgradestatus)
        self._unsub_poll: CALLBACK_TYPE | None = None
        self._install_started: float = 0.0
        self._install_activity = False
        self._version_at_start: str | None = None
        self._api_lost = False
        # Privilège upgradestatus absent : on ne sonde plus l'API, mais le
        # garde-fou de durée continue de tourner
        self._status_forbidden = False
        self._removed = False
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

        Ordre de recherche de la cible : si le statut vaut "upgrade" et que
        upgrade_major_version est renseignée, c'est elle (OPNsense renvoie
        alors product_latest == version installée, dernière de la branche
        courante). Sinon product_latest, mais seulement s'il diffère de
        l'installée. À défaut, "<installée> (mise à jour disponible)" :
        valeur différente de l'installée, uniquement affichable. Limite :
        ce n'est pas une vraie version, la fin d'installation ne s'appuie
        donc jamais dessus.
        """
        prod = self._product()
        installed = self.installed_version
        if not prod:
            return installed
        fw = self._fw()
        status = fw.get("status") if fw else None
        if status == "upgrade":
            major = fw.get("upgrade_major_version") if fw else None
            if major and str(major) != "0":
                return str(major)
        latest = prod.get("product_latest")
        if latest and latest != "0" and latest != installed:
            return latest
        if status in ("update", "upgrade"):
            # Une MAJ existe mais on ne connaît pas la version exacte.
            return f"{installed or '?'} (mise à jour disponible)"
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
        Statut "upgrade" = mise à jour majeure -> /firmware/upgrade.
        """
        _LOGGER.warning(
            "Déclenchement de la mise à jour OPNsense via HA - "
            "le firewall peut redémarrer pendant l'opération"
        )
        fw = self._fw()
        major = bool(fw and fw.get("status") == "upgrade")
        self._installing = True
        prod = self._product()
        # product_version brut : on ignore le repli product_series
        self._version_at_start = (prod or {}).get("product_version") or None
        self._api_lost = False
        self._status_forbidden = False
        self._install_started = time.monotonic()
        self._install_activity = False
        self.async_write_ha_state()

        try:
            if major:
                result = await self.coordinator.client.async_run_upgrade()
            else:
                result = await self.coordinator.client.async_run_update()
            status = result.get("status") if isinstance(result, dict) else None
            if status is not None and status != "ok":
                raise OPNsenseApiError(f"réponse inattendue : {status}")
        except OPNsenseApiError as err:
            _LOGGER.error("Erreur lors de l'installation : %s", err)
            self._installing = False
            self.async_write_ha_state()
            raise HomeAssistantError(
                f"Échec du lancement de la mise à jour OPNsense : {err}"
            ) from err

        # La fin est détectée par le sondage de /firmware/upgradestatus.
        # Entité retirée pendant l'await : on n'arme pas de timer orphelin.
        if not self._removed:
            self._start_polling()
        await self.coordinator.async_request_refresh()

    def _start_polling(self) -> None:
        """Démarre le sondage de progression (un seul à la fois)."""
        self._stop_polling()
        self._unsub_poll = async_track_time_interval(
            self.hass, self._async_poll_upgrade, UPGRADE_POLL_INTERVAL
        )

    def _stop_polling(self) -> None:
        """Arrête le sondage s'il est actif."""
        if self._unsub_poll is not None:
            self._unsub_poll()
            self._unsub_poll = None

    def _finish_install(self) -> None:
        """Clôt l'état d'installation."""
        self._installing = False
        self._stop_polling()
        self.async_write_ha_state()

    async def _async_poll_upgrade(self, _now: Any) -> None:
        """Interroge /firmware/upgradestatus et clôt l'installation si fini."""
        if not self._installing:
            self._stop_polling()
            return
        elapsed = time.monotonic() - self._install_started
        if elapsed > UPGRADE_MAX_DURATION:
            _LOGGER.warning(
                "Mise à jour OPNsense : durée maximale dépassée (%d min), "
                "fin du suivi",
                UPGRADE_MAX_DURATION // 60,
            )
            self._finish_install()
            return
        if self._status_forbidden:
            # Plus d'appel API : seule la durée maximale ou un changement de
            # version (_handle_coordinator_update) clôt l'installation
            return
        try:
            result = await self.coordinator.client.async_get_upgrade_status()
        except (OPNsenseForbiddenError, OPNsenseAuthError) as err:
            _LOGGER.warning(
                "Suivi de la mise à jour impossible (privilège upgradestatus "
                "manquant ?) : %s. Fin détectée par changement de version "
                "ou durée maximale",
                err,
            )
            self._status_forbidden = True
            return
        except OPNsenseApiError as err:
            # Redémarrage du firewall : API injoignable, ce n'est pas une erreur
            self._install_activity = True
            self._api_lost = True
            _LOGGER.debug("upgradestatus injoignable (redémarrage ?) : %s", err)
            return
        status = result.get("status") if isinstance(result, dict) else None
        if status == "running":
            self._install_activity = True
            return
        if status not in ("done", "reboot", "error"):
            return
        if not self._install_activity and elapsed < UPGRADE_STALE_GRACE:
            # Probablement le résultat d'une exécution précédente
            return
        if status == "error":
            log = str(result.get("log") or "")[-1000:]
            if self._api_lost and not log:
                _LOGGER.warning(
                    "Mise à jour OPNsense : suivi perdu après redémarrage"
                )
            else:
                _LOGGER.error(
                    "La mise à jour OPNsense a échoué. Journal : %s",
                    log or "indisponible",
                )
        else:
            _LOGGER.info("Mise à jour OPNsense terminée (statut : %s)", status)
        self._finish_install()
        await self.coordinator.async_request_refresh()

    async def async_will_remove_from_hass(self) -> None:
        """Annule le sondage au retrait de l'entité."""
        self._removed = True
        self._stop_polling()
        await super().async_will_remove_from_hass()

    @property
    def available(self) -> bool:
        """Disponible si le coordinator a des données firmware."""
        return super().available and self._fw() is not None

    def _handle_coordinator_update(self) -> None:
        """Clôt aussi l'installation si la version installée a changé.

        NB : le bon hook de CoordinatorEntity est `_handle_coordinator_update`
        (et non `_coordinator_updated`, qui n'est jamais appelé). Complément
        du sondage upgradestatus ; on ne compare plus à latest_version
        (valeur parfois non fiable).
        """
        prod = self._product()
        # product_version brut non vide uniquement (pas le repli series)
        installed = (prod or {}).get("product_version")
        if (
            self._installing
            and self._version_at_start
            and installed
            and installed != self._version_at_start
        ):
            self._installing = False
            self._stop_polling()
        super()._handle_coordinator_update()
