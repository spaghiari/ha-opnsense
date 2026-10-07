"""Constantes pour l'intégration OPNsense custom."""
from __future__ import annotations

DOMAIN = "opnsense_custom"

# Clés de configuration
CONF_HOST = "host"
CONF_PORT = "port"
CONF_API_KEY = "api_key"
CONF_API_SECRET = "api_secret"
CONF_VERIFY_SSL = "verify_ssl"
CONF_SCAN_INTERVAL = "scan_interval"
# Device de l'interface WAN choisie par l'utilisateur (ex: "igc0").
# Vide => auto-détection (route par défaut / IP publique / description).
CONF_WAN_INTERFACE = "wan_interface"
# Crée automatiquement un dashboard "OPNsense" dans la barre latérale.
CONF_CREATE_DASHBOARD = "create_dashboard"
DEFAULT_CREATE_DASHBOARD = True

# ---- Alertes intégrées (étape "Notifications" des options) ----
CONF_ALERTS = "alerts"
CONF_NOTIFY_TARGETS = "notify_targets"
CONF_NOTIFY_PERSISTENT = "notify_persistent"
CONF_WAN_DOWN_DELAY = "wan_down_delay"
CONF_LATENCY_THRESHOLD = "latency_threshold"
CONF_LATENCY_DURATION = "latency_duration"
ALERT_WAN = "wan"
ALERT_LATENCY = "latency"
ALERT_FIRMWARE = "firmware"
ALERT_SERVICES = "services"
ALERT_VPN = "vpn"
ALERT_DISK = "disk"
ALERT_TYPES = (
    ALERT_WAN, ALERT_LATENCY, ALERT_FIRMWARE, ALERT_SERVICES, ALERT_VPN,
    ALERT_DISK,
)
# Pré-cochées dans le formulaire ; rien n'est envoyé tant que l'utilisateur
# n'a pas validé l'étape (options absentes = aucune alerte).
DEFAULT_ALERTS = [ALERT_WAN, ALERT_LATENCY, ALERT_FIRMWARE, ALERT_SERVICES,
                  ALERT_VPN]
DEFAULT_NOTIFY_PERSISTENT = True
DEFAULT_WAN_DOWN_DELAY = 1        # minutes
DEFAULT_LATENCY_THRESHOLD = 100   # ms
DEFAULT_LATENCY_DURATION = 5      # minutes
# Disque : alerte au-dessus de DISK_ALERT_PCT, ré-armée sous DISK_REARM_PCT.
DISK_ALERT_PCT = 90
DISK_REARM_PCT = 85
# url_path de base du dashboard auto-généré (suffixé "-2", "-3"... pour les
# firewalls suivants).
DASHBOARD_URL_PATH = "opnsense"
# Version du gabarit de dashboard. Incrémenter pour re-semer le design
# par défaut au prochain chargement (les éditions manuelles seront alors
# remplacées - le dashboard auto est "géré" par l'intégration).
DASHBOARD_TEMPLATE_VERSION = 5

# Valeurs par défaut
DEFAULT_PORT = 443
DEFAULT_VERIFY_SSL = False
DEFAULT_SCAN_INTERVAL = 60
MIN_SCAN_INTERVAL = 30
MAX_SCAN_INTERVAL = 600
# Valeur sentinelle "laisser l'intégration auto-détecter le WAN"
WAN_AUTO = "__auto__"

# Plateformes que l'intégration expose
PLATFORMS = ["sensor", "binary_sensor", "button", "update"]

# Endpoints API OPNsense (validés sur 26.1.8 avec privilèges restreints)
API_ENDPOINTS = {
    "firmware_status": "/api/core/firmware/status",
    "firmware_check": "/api/core/firmware/check",
    "firmware_update": "/api/core/firmware/update",
    "system_information": "/api/diagnostics/system/system_information",
    "system_resources": "/api/diagnostics/system/system_resources",
    "system_disk": "/api/diagnostics/system/system_disk",
    "system_time": "/api/diagnostics/system/system_time",
    "cpu_type": "/api/diagnostics/cpu_usage/getCPUType",
    "interfaces": "/api/interfaces/overview/interfacesInfo",
    "traffic_totals": "/api/diagnostics/traffic/interface",
    # {iface} = identifiant de config OPNsense de l'interface WAN résolue
    # ("wan", "opt1"...), fourni par le coordinator.
    "traffic_wan": "/api/diagnostics/traffic/top/{iface}",
    # Optionnels (privilèges "Status: Gateways" / "Status: Services") :
    # sans eux, seuls les capteurs latence/pertes/services restent vides.
    "gateway_status": "/api/routes/gateway/status",
    "services": "/api/core/service/search",
}

# Identifiant de config OPNsense par défaut de l'interface WAN (avant la
# première résolution par le coordinator).
DEFAULT_WAN_IDENTIFIER = "wan"

# Manufacturer / model pour DeviceInfo
MANUFACTURER = "Deciso"
DEFAULT_MODEL = "OPNsense Firewall"

# Timeout des requêtes HTTP
HTTP_TIMEOUT = 15
