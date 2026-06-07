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
    # NB : top/wan cible l'interface OPNsense nommée littéralement "wan".
    # Les setups dont le WAN porte un autre nom de config ne verront pas le
    # débit temps réel / top destinations (limite connue, cf. backlog).
    "traffic_wan": "/api/diagnostics/traffic/top/wan",
}

# Manufacturer / model pour DeviceInfo
MANUFACTURER = "Deciso"
DEFAULT_MODEL = "OPNsense Firewall"

# Timeout des requêtes HTTP
HTTP_TIMEOUT = 15
