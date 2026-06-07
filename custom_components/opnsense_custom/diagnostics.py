"""Diagnostics téléchargeables pour OPNsense custom.

Permet d'exporter l'état de l'intégration (config + dernier snapshot du
coordinator) depuis l'UI HA, avec masquage automatique des secrets.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_API_KEY, CONF_API_SECRET, CONF_HOST, DOMAIN
from .coordinator import OPNsenseDataCoordinator

# Clés à ne jamais exporter en clair
TO_REDACT = {CONF_API_KEY, CONF_API_SECRET, CONF_HOST}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Renvoie les diagnostics d'une ConfigEntry (secrets masqués)."""
    coordinator: OPNsenseDataCoordinator = hass.data[DOMAIN][entry.entry_id]

    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "resolved_wan_device": (
            coordinator.data.get("_wan_device") if coordinator.data else None
        ),
        "coordinator_data": async_redact_data(
            coordinator.data or {}, TO_REDACT
        ),
    }
