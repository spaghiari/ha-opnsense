"""Config flow pour OPNsense custom."""
from __future__ import annotations

import logging
from collections.abc import Mapping
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
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

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
    CONF_WAN_INTERFACE,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    WAN_AUTO,
)
from .coordinator import resolve_wan_device

_LOGGER = logging.getLogger(__name__)

# Schéma du formulaire de connexion (réutilisé par user / reauth / reconfigure)
def _credentials_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Construit le schéma des credentials, pré-rempli si `defaults` fourni."""
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_HOST, default=defaults.get(CONF_HOST, vol.UNDEFINED)
            ): str,
            vol.Required(
                CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)
            ): vol.All(int, vol.Range(min=1, max=65535)),
            vol.Required(CONF_API_KEY): str,
            vol.Required(CONF_API_SECRET): str,
            vol.Required(
                CONF_VERIFY_SSL,
                default=defaults.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL),
            ): bool,
        }
    )


def _wan_select_options(rows: Any) -> list[SelectOptionDict]:
    """Construit la liste déroulante des interfaces (+ option auto)."""
    options: list[SelectOptionDict] = [
        SelectOptionDict(value=WAN_AUTO, label="Auto-détection (recommandé)")
    ]
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            device = row.get("device")
            if not device:
                continue
            desc = row.get("description") or device
            options.append(
                SelectOptionDict(value=device, label=f"{desc} ({device})")
            )
    return options


async def _validate_and_get_interfaces(
    hass, user_input: dict[str, Any]
) -> tuple[dict[str, Any], Any]:
    """Teste les credentials et renvoie (system_info, interfaces_rows).

    Lève les exceptions OPNsense* en cas d'échec.
    """
    session = async_get_clientsession(
        hass, verify_ssl=user_input[CONF_VERIFY_SSL]
    )
    client = OPNsenseApiClient(
        host=user_input[CONF_HOST],
        port=user_input[CONF_PORT],
        api_key=user_input[CONF_API_KEY],
        api_secret=user_input[CONF_API_SECRET],
        session=session,
        verify_ssl=user_input[CONF_VERIFY_SSL],
    )
    info = await client.async_test_credentials()
    # Best-effort : récupère les interfaces pour proposer le choix du WAN.
    rows: Any = None
    try:
        interfaces = await client.get("interfaces")
        rows = interfaces.get("rows") if isinstance(interfaces, dict) else None
    except OPNsenseApiError as err:
        _LOGGER.debug("Liste des interfaces indisponible : %s", err)
    return info, rows


class OPNsenseConfigFlow(ConfigFlow, domain=DOMAIN):
    """Gère l'ajout d'une nouvelle instance OPNsense via l'UI."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise l'état inter-étapes."""
        self._data: dict[str, Any] = {}
        self._wan_rows: Any = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Première étape : saisie des credentials par l'utilisateur."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                info, rows = await _validate_and_get_interfaces(
                    self.hass, user_input
                )
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

                self._data = user_input
                self._wan_rows = rows
                return await self.async_step_wan()

        return self.async_show_form(
            step_id="user",
            data_schema=_credentials_schema(),
            errors=errors,
        )

    async def async_step_wan(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Deuxième étape : choix de l'interface WAN à surveiller."""
        if user_input is not None:
            return self.async_create_entry(
                title=f"OPNsense ({self.unique_id})",
                data=self._data,
                options={
                    CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
                    CONF_WAN_INTERFACE: user_input[CONF_WAN_INTERFACE],
                },
            )

        # Pré-sélectionne l'interface auto-détectée (sinon "Auto")
        detected = resolve_wan_device(self._wan_rows, WAN_AUTO) or WAN_AUTO
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_WAN_INTERFACE, default=detected
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=_wan_select_options(self._wan_rows),
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="wan", data_schema=schema)

    # ------------------------------------------------------------------
    #  Ré-authentification (clé API tournée / révoquée)
    # ------------------------------------------------------------------
    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Point d'entrée déclenché par ConfigEntryAuthFailed."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Demande de nouveaux credentials et met à jour l'entry existante."""
        entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        assert entry is not None
        errors: dict[str, str] = {}

        if user_input is not None:
            merged = {**entry.data, **user_input}
            try:
                info, _ = await _validate_and_get_interfaces(self.hass, merged)
            except OPNsenseAuthError:
                errors["base"] = "invalid_auth"
            except OPNsenseForbiddenError:
                errors["base"] = "insufficient_privileges"
            except OPNsenseApiError:
                errors["base"] = "cannot_connect"
            else:
                # Garde : on doit ré-authentifier LE MÊME firewall, pas un autre
                new_id = info.get("name", merged[CONF_HOST]).lower()
                if entry.unique_id and entry.unique_id != new_id:
                    return self.async_abort(reason="unique_id_mismatch")
                return self.async_update_reload_and_abort(entry, data=merged)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_credentials_schema(entry.data),
            errors=errors,
        )

    # ------------------------------------------------------------------
    #  Reconfiguration (changer host/port/clé sans tout supprimer)
    # ------------------------------------------------------------------
    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Permet de modifier les paramètres de connexion."""
        entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        assert entry is not None
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                info, _ = await _validate_and_get_interfaces(
                    self.hass, user_input
                )
            except OPNsenseAuthError:
                errors["base"] = "invalid_auth"
            except OPNsenseForbiddenError:
                errors["base"] = "insufficient_privileges"
            except OPNsenseApiError:
                errors["base"] = "cannot_connect"
            else:
                # Garde : empêche de pointer l'entry vers un AUTRE firewall
                new_id = info.get("name", user_input[CONF_HOST]).lower()
                if entry.unique_id and entry.unique_id != new_id:
                    return self.async_abort(reason="unique_id_mismatch")
                return self.async_update_reload_and_abort(
                    entry, data=user_input
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_credentials_schema(entry.data),
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
    """Permet de modifier l'intervalle de polling et l'interface WAN."""

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
        current_wan = self._entry.options.get(CONF_WAN_INTERFACE, WAN_AUTO)

        # Récupère la liste des interfaces depuis les données déjà pollées
        rows = None
        coordinator = (
            self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
        )
        if coordinator is not None and coordinator.data:
            rows = (coordinator.data.get("interfaces") or {}).get("rows")

        options_schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL, default=current_interval
                ): vol.All(
                    int,
                    vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL),
                ),
                vol.Required(
                    CONF_WAN_INTERFACE, default=current_wan
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=_wan_select_options(rows),
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=options_schema,
        )
