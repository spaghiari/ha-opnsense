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
from homeassistant.data_entry_flow import section
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
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
    ALERT_TYPES,
    CONF_ALERTS,
    CONF_API_KEY,
    CONF_API_SECRET,
    CONF_CREATE_DASHBOARD,
    CONF_DASHBOARD_THEME,
    CONF_FAST_INTERVAL,
    CONF_HOST,
    CONF_LATENCY_DURATION,
    CONF_LATENCY_THRESHOLD,
    CONF_LIVE_PUBLISH,
    CONF_NOTIFY_PERSISTENT,
    CONF_NOTIFY_TARGETS,
    CONF_PORT,
    CONF_REALTIME,
    CONF_SCAN_INTERVAL,
    CONF_TEMP_THRESHOLD,
    CONF_VERIFY_SSL,
    CONF_WAN_DOWN_DELAY,
    CONF_WAN_EXCLUDE,
    CONF_WAN_INTERFACE,
    DASHBOARD_THEMES,
    DEFAULT_ALERTS,
    DEFAULT_CREATE_DASHBOARD,
    DEFAULT_DASHBOARD_THEME,
    DEFAULT_FAST_INTERVAL,
    DEFAULT_LATENCY_DURATION,
    DEFAULT_LATENCY_THRESHOLD,
    DEFAULT_LIVE_PUBLISH,
    DEFAULT_NOTIFY_PERSISTENT,
    DEFAULT_PORT,
    DEFAULT_REALTIME,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TEMP_THRESHOLD,
    DEFAULT_VERIFY_SSL,
    DEFAULT_WAN_DOWN_DELAY,
    DOMAIN,
    MAX_FAST_INTERVAL,
    MAX_LIVE_PUBLISH,
    MAX_SCAN_INTERVAL,
    MIN_FAST_INTERVAL,
    MIN_LIVE_PUBLISH,
    MIN_SCAN_INTERVAL,
    WAN_AUTO,
)
from .coordinator import resolve_wan_device
from .wans import wan_links

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
                    CONF_CREATE_DASHBOARD: DEFAULT_CREATE_DASHBOARD,
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
    """Configuration après installation : un menu, trois écrans thématiques.

    Chaque écran enregistre ses propres réglages et conserve les autres.
    """

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Mémorise l'entry pour relire les options actuelles."""
        self._entry = config_entry

    def _save(self, user_input: dict[str, Any]) -> ConfigFlowResult:
        """Enregistre l'écran courant en conservant les autres options."""
        return self.async_create_entry(
            title="", data={**self._entry.options, **user_input}
        )

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Menu principal."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["refresh", "dashboard", "notifications"],
        )

    async def async_step_refresh(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Rafraîchissement : temps réel, rapide, lent + interface WAN."""
        if user_input is not None:
            return self._save(user_input)

        opts = self._entry.options
        rows = None
        links: list[dict] = []
        coordinator = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
        if coordinator is not None and coordinator.data:
            # Les interfaces sont lues par le polling rapide (vue fusionnée).
            merged = coordinator.merged
            rows = (merged.get("interfaces") or {}).get("rows")
            # Tous les liens WAN, y compris ceux exclus aujourd'hui.
            if merged.get("gateways"):
                links = wan_links(merged)

        fields: dict[Any, Any] = {}
        if len(links) >= 2:
            # Multi-WAN : liens à ignorer (ex. une 4G de secours qu'on ne
            # veut pas voir « coupée » à chaque mise en veille).
            fields[vol.Optional(
                CONF_WAN_EXCLUDE, default=opts.get(CONF_WAN_EXCLUDE, [])
            )] = SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(
                            value=link["id"],
                            label=f"{link['name']} ({link['device'] or link['id']})",
                        )
                        for link in links
                    ],
                    multiple=True,
                    mode=SelectSelectorMode.LIST,
                )
            )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_REALTIME, default=opts.get(CONF_REALTIME, DEFAULT_REALTIME)
                ): BooleanSelector(),
                vol.Required(
                    CONF_LIVE_PUBLISH,
                    default=opts.get(CONF_LIVE_PUBLISH, DEFAULT_LIVE_PUBLISH),
                ): _seconds(MIN_LIVE_PUBLISH, MAX_LIVE_PUBLISH),
                vol.Required(
                    CONF_FAST_INTERVAL,
                    default=opts.get(CONF_FAST_INTERVAL, DEFAULT_FAST_INTERVAL),
                ): _seconds(MIN_FAST_INTERVAL, MAX_FAST_INTERVAL),
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=opts.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): _seconds(MIN_SCAN_INTERVAL, MAX_SCAN_INTERVAL, step=30),
                vol.Required(
                    CONF_WAN_INTERFACE,
                    default=opts.get(CONF_WAN_INTERFACE, WAN_AUTO),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=_wan_select_options(rows),
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                **fields,
            }
        )
        return self.async_show_form(step_id="refresh", data_schema=schema)

    async def async_step_dashboard(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Dashboard : création automatique et thème."""
        if user_input is not None:
            return self._save(user_input)

        opts = self._entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_CREATE_DASHBOARD,
                    default=opts.get(CONF_CREATE_DASHBOARD, DEFAULT_CREATE_DASHBOARD),
                ): BooleanSelector(),
                vol.Required(
                    CONF_DASHBOARD_THEME,
                    default=opts.get(CONF_DASHBOARD_THEME, DEFAULT_DASHBOARD_THEME),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=list(DASHBOARD_THEMES),
                        mode=SelectSelectorMode.LIST,
                        translation_key=CONF_DASHBOARD_THEME,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="dashboard", data_schema=schema)

    async def async_step_notifications(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Notifications : alertes, destinataires et seuils (section repliée)."""
        if user_input is not None:
            # La section "Seuils" arrive imbriquée : on l'aplatit.
            flat = dict(user_input)
            flat.update(flat.pop("thresholds", {}) or {})
            return self._save(flat)

        opts = self._entry.options
        thresholds = vol.Schema(
            {
                vol.Required(
                    CONF_WAN_DOWN_DELAY,
                    default=opts.get(CONF_WAN_DOWN_DELAY, DEFAULT_WAN_DOWN_DELAY),
                ): _number(1, 60, "min"),
                vol.Required(
                    CONF_LATENCY_THRESHOLD,
                    default=opts.get(CONF_LATENCY_THRESHOLD, DEFAULT_LATENCY_THRESHOLD),
                ): _number(10, 1000, "ms", step=10),
                vol.Required(
                    CONF_LATENCY_DURATION,
                    default=opts.get(CONF_LATENCY_DURATION, DEFAULT_LATENCY_DURATION),
                ): _number(1, 60, "min"),
                vol.Required(
                    CONF_TEMP_THRESHOLD,
                    default=opts.get(CONF_TEMP_THRESHOLD, DEFAULT_TEMP_THRESHOLD),
                ): _number(40, 110, "°C"),
            }
        )
        schema = vol.Schema(
            {
                # Sans option enregistrée, on pré-coche la sélection recommandée.
                vol.Optional(
                    CONF_ALERTS, default=opts.get(CONF_ALERTS, DEFAULT_ALERTS)
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=list(ALERT_TYPES),
                        multiple=True,
                        mode=SelectSelectorMode.LIST,
                        translation_key=CONF_ALERTS,
                    )
                ),
                vol.Optional(
                    CONF_NOTIFY_TARGETS, default=opts.get(CONF_NOTIFY_TARGETS, [])
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=_notify_options(self.hass),
                        multiple=True,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(
                    CONF_NOTIFY_PERSISTENT,
                    default=opts.get(CONF_NOTIFY_PERSISTENT, DEFAULT_NOTIFY_PERSISTENT),
                ): BooleanSelector(),
                vol.Required("thresholds"): section(thresholds, {"collapsed": True}),
            }
        )
        return self.async_show_form(step_id="notifications", data_schema=schema)


def _number(minimum: int, maximum: int, unit: str, step: int = 1) -> vol.All:
    """Curseur numérique avec unité (valeur enregistrée en entier)."""
    return vol.All(
        NumberSelector(
            NumberSelectorConfig(
                min=minimum, max=maximum, step=step,
                unit_of_measurement=unit, mode=NumberSelectorMode.SLIDER,
            )
        ),
        vol.Coerce(int),
    )


def _seconds(minimum: int, maximum: int, step: int = 1) -> vol.All:
    """Curseur en secondes."""
    return _number(minimum, maximum, "s", step)


# Services notify génériques qu'on ne propose pas comme destinataires.
_NOTIFY_EXCLUDED = {"notify", "persistent_notification", "send_message"}


def _notify_options(hass) -> list[SelectOptionDict]:
    """Services notify disponibles, téléphones (mobile_app) en premier."""
    services = sorted(
        s for s in hass.services.async_services().get("notify", {})
        if s not in _NOTIFY_EXCLUDED
    )
    services.sort(key=lambda s: not s.startswith("mobile_app_"))
    options = []
    for service in services:
        if service.startswith("mobile_app_"):
            label = "Téléphone · " + service.removeprefix("mobile_app_").replace(
                "_", " "
            ).title()
        else:
            label = f"notify.{service}"
        options.append(SelectOptionDict(value=service, label=label))
    return options
