"""Alertes « Réparations » quand un droit API OPNsense manque.

Sans certains droits, l'intégration se dégrade sans planter (capteurs vides,
retour au mode un seul WAN...). Pour que ce ne soit pas silencieux, chaque
droit refusé (HTTP 403) ouvre une alerte dans Paramètres → Réparations, qui
nomme le droit à cocher et ce qu'il débloque. L'alerte disparaît d'elle-même
dès que l'endpoint répond de nouveau.
"""
from __future__ import annotations

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import issue_registry as ir

from .const import CONF_CREATE_DASHBOARD, DEFAULT_CREATE_DASHBOARD, DOMAIN

# Droit OPNsense (nom affiché dans Système → Accès → Utilisateurs) ->
# endpoints qu'il ouvre et ce qu'ils débloquent dans l'intégration.
PRIVILEGES: dict[str, dict] = {
    "gateways": {
        "privilege": "System: Gateways",
        "endpoints": ("gateways", "gateway_status"),
        "impact": "liens WAN et multi-WAN, détection des pannes opérateur, "
                  "latence et pertes de paquets",
    },
    "gateway_groups": {
        "privilege": "System: Gateway Groups",
        "endpoints": ("gateway_groups",),
        "impact": "capteurs des groupes de passerelles et alertes de bascule "
                  "(utile seulement si tu as des groupes de passerelles)",
    },
    "services": {
        "privilege": "Status: Services",
        "endpoints": ("services",),
        "impact": "capteur et alertes des services arrêtés",
    },
    "traffic": {
        "privilege": "Reporting: Traffic",
        "endpoints": ("traffic_totals", "traffic_wan", "traffic_stream"),
        "impact": "débit et volumes WAN, top destinations",
    },
}


def sync_privilege_issues(
    hass: HomeAssistant | None, entry: ConfigEntry, forbidden: set[str]
) -> None:
    """Ouvre / ferme une alerte par droit selon les endpoints refusés."""
    if hass is None:
        return
    for slug, info in PRIVILEGES.items():
        issue_id = f"missing_privilege_{slug}_{entry.entry_id}"
        if forbidden.intersection(info["endpoints"]):
            ir.async_create_issue(
                hass, DOMAIN, issue_id,
                is_fixable=False,
                is_persistent=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="missing_privilege",
                translation_placeholders={
                    "privilege": info["privilege"],
                    "impact": info["impact"],
                    "host": entry.title or entry.data.get("host", "OPNsense"),
                },
            )
        else:
            ir.async_delete_issue(hass, DOMAIN, issue_id)


def delete_privilege_issues(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Supprime toutes les alertes de droits de cette entry."""
    for slug in PRIVILEGES:
        issue_id = f"missing_privilege_{slug}_{entry.entry_id}"
        ir.async_delete_issue(hass, DOMAIN, issue_id)


# ---- Dashboard retouché : jamais écrasé sans accord ----


def _dashboard_issue_id(entry: ConfigEntry) -> str:
    return f"dashboard_edited_{entry.entry_id}"


def create_dashboard_issue(
    hass: HomeAssistant, entry: ConfigEntry, url_path: str
) -> None:
    """Alerte : le gabarit a évolué mais le dashboard a été modifié à la main.

    Réparable : la confirmation réinitialise le dashboard vers le nouveau
    gabarit (les modifications sont perdues).
    """
    ir.async_create_issue(
        hass, DOMAIN, _dashboard_issue_id(entry),
        is_fixable=True,
        is_persistent=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="dashboard_edited",
        translation_placeholders={
            "url_path": url_path,
            "host": entry.title or entry.data.get("host", "OPNsense"),
        },
        data={"entry_id": entry.entry_id, "url_path": url_path},
    )


def delete_dashboard_issue(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Ferme l'alerte dashboard (re-semé ou entrée supprimée)."""
    ir.async_delete_issue(hass, DOMAIN, _dashboard_issue_id(entry))


class DashboardResetFlow(RepairsFlow):
    """Confirmation : réinitialiser le dashboard vers le nouveau gabarit."""

    def __init__(self, entry_id: str, url_path: str) -> None:
        self._entry_id = entry_id
        self._url_path = url_path

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> FlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="confirm",
                data_schema=vol.Schema({}),
                description_placeholders={"url_path": self._url_path},
            )
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None or entry.state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="entry_not_loaded")
        if not entry.options.get(CONF_CREATE_DASHBOARD, DEFAULT_CREATE_DASHBOARD):
            return self.async_abort(reason="dashboard_disabled")
        # Import tardif : dashboard.py importe ce module.
        from .dashboard import async_register_dashboard  # noqa: PLC0415

        await async_register_dashboard(self.hass, entry, force=True, refresh=True)
        # Le re-semis ferme l'alerte ; si elle est encore là, il a échoué.
        if ir.async_get(self.hass).async_get_issue(
            DOMAIN, _dashboard_issue_id(entry)
        ):
            return self.async_abort(reason="reset_failed")
        return self.async_create_entry(data={})


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, str | int | float | None] | None
) -> RepairsFlow:
    """Point d'entrée de la plateforme repairs."""
    data = data or {}
    return DashboardResetFlow(
        str(data.get("entry_id", "")), str(data.get("url_path", ""))
    )
