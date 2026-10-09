"""Alertes « Réparations » quand un droit API OPNsense manque.

Sans certains droits, l'intégration se dégrade sans planter (capteurs vides,
retour au mode un seul WAN...). Pour que ce ne soit pas silencieux, chaque
droit refusé (HTTP 403) ouvre une alerte dans Paramètres → Réparations, qui
nomme le droit à cocher et ce qu'il débloque. L'alerte disparaît d'elle-même
dès que l'endpoint répond de nouveau.
"""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN

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
