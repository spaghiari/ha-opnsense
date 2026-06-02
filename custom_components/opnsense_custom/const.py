"""Constantes pour l'intégration OPNsense custom."""
from __future__ import annotations

from datetime import timedelta

DOMAIN = "opnsense_custom"

# Clés de configuration
CONF_HOST = "host"
CONF_PORT = "port"
CONF_API_KEY = "api_key"
CONF_API_SECRET = "api_secret"
CONF_VERIFY_SSL = "verify_ssl"
CONF_SCAN_INTERVAL = "scan_interval"

# Valeurs par défaut
DEFAULT_PORT = 443
DEFAULT_VERIFY_SSL = False
DEFAULT_SCAN_INTERVAL = 60
MIN_SCAN_INTERVAL = 30
MAX_SCAN_INTERVAL = 600

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
    "traffic_wan": "/api/diagnostics/traffic/top/wan",
}

# Manufacturer / model pour DeviceInfo
MANUFACTURER = "Ziko"
DEFAULT_MODEL = "OPNsense Firewall"

# Timeout des requêtes HTTP
HTTP_TIMEOUT = 15
