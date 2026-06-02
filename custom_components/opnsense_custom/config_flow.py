"""Config flow pour OPNsense custom."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    OPNsenseApiClient,
    OPNsenseApiError,
    OPNsenseAuthError,
    OPNsenseForbiddenError,
)
from .const import (
    CONF_API_KEY,
    CONF_API_SECRET,
    CONF_HOST,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    CONF_VERIFY_SSL,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

# Schéma du formulaire initial
USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.All(
            int, vol.Range(min=1, max=65535)
        ),
        vol.Required(CONF_API_KEY): str,
        vol.Required(CONF_API_SECRET): str,
        vol.Required(CONF_VERIFY_SSL, default=DEFAULT_VERIFY_SSL): bool,
    }
)


class OPNsenseConfigFlow(ConfigFlow, domain=DOMAIN):
    """Gère l'ajout d'une nouvelle instance OPNsense via l'UI."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Première étape : saisie des credentials par l'utilisateur."""
        errors: dict[str, str] = {}

        if user_input is not None:
            session = async_get_clientsession(
                self.hass, verify_ssl=user_input[CONF_VERIFY_SSL]
            )
            client = OPNsenseApiClient(
                host=user_input[CONF_HOST],
                port=user_input[CONF_PORT],
                api_key=user_input[CONF_API_KEY],
                api_secret=user_input[CONF_API_SECRET],
                session=session,
                verify_ssl=user_input[CONF_VERIFY_SSL],
            )

            try:
                info = await client.async_test_credentials()
            except OPNsenseAuthError:
                errors["base"] = "invalid_auth"
            except OPNsenseForbiddenError:
                errors["base"] = "insufficient_privileges"
            except OPNsenseApiError as err:
                _LOGGER.error("Erreur lors du test : %s", err)
                errors["base"] = "cannot_connect"
            else:
                # Empêche d'ajouter deux fois le même firewall
                hostname = info.get("name", user_input[CONF_HOST])
                await self.async_set_unique_id(hostname.lower())
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=f"OPNsense ({hostname})",
                    data=user_input,
                    options={CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL},
                )

        return self.async_show_form(
            step_id="user",
            data_schema=USER_SCHEMA,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> OptionsFlow:
        """Renvoie l'OptionsFlow pour modifier les paramètres après installation."""
        return OPNsenseOptionsFlow(config_entry)


class OPNsenseOptionsFlow(OptionsFlow):
    """Permet de modifier l'intervalle de polling sans réinstaller."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Mémorise l'entry pour relire les options actuelles."""
        self._entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Affiche / sauvegarde les options modifiables."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_interval = self._entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )

        options_schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL, default=current_interval
                ): vol.All(
                    int,
                    vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL),
                ),
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=options_schema,
        )
