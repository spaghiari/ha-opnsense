"""Intégration OPNsense custom pour Home Assistant."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import OPNsenseApiClient
from .const import (
    CONF_API_KEY,
    CONF_API_SECRET,
    CONF_HOST,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    CONF_VERIFY_SSL,
    CONF_WAN_INTERFACE,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
    PLATFORMS,
    WAN_AUTO,
)
from .coordinator import OPNsenseDataCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Initialise l'intégration à partir d'une ConfigEntry.

    Étapes :
    1. Récupération des credentials depuis l'entry
    2. Création du client API
    3. Création du coordinator + premier refresh
    4. Stockage dans hass.data[DOMAIN][entry_id]
    5. Forwarding aux plateformes (sensor, button, update...)
    """
    host: str = entry.data[CONF_HOST]
    port: int = entry.data.get(CONF_PORT, DEFAULT_PORT)
    api_key: str = entry.data[CONF_API_KEY]
    api_secret: str = entry.data[CONF_API_SECRET]
    verify_ssl: bool = entry.data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)

    # L'intervalle de polling et le choix WAN sont dans entry.options
    # (modifiables via OptionsFlow sans réinstaller).
    scan_interval: int = entry.options.get(
        CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
    )
    wan_interface: str = entry.options.get(CONF_WAN_INTERFACE, WAN_AUTO)

    session = async_get_clientsession(hass, verify_ssl=verify_ssl)
    client = OPNsenseApiClient(
        host=host,
        port=port,
        api_key=api_key,
        api_secret=api_secret,
        session=session,
        verify_ssl=verify_ssl,
    )

    coordinator = OPNsenseDataCoordinator(
        hass=hass,
        client=client,
        scan_interval=scan_interval,
        entry=entry,
        wan_interface=wan_interface,
    )

    # Premier refresh - si ça échoue, on remonte l'erreur et HA ne charge pas
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    # Listener pour recharger si les options changent (ex: nouveau scan_interval)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    # Charge les plateformes (sensor, binary_sensor, button, update)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Décharge proprement l'intégration."""
    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, PLATFORMS
    )
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


async def _async_options_updated(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Recharge l'intégration quand les options changent."""
    await hass.config_entries.async_reload(entry.entry_id)
