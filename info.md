# OPNsense for Home Assistant

A custom integration to monitor your **OPNsense firewall** from Home Assistant.

## Features

- 📊 **System metrics**: CPU load, RAM, disk usage, uptime
- 🌐 **WAN monitoring**: public IPv4/IPv6, real-time throughput, total transferred
- 🔝 **Top destinations**: see which hosts consume the most bandwidth
- 📦 **Firmware updates**: native `update` entity with one-click install button
- 🔔 **Update notifications**: built-in binary sensor for automations
- 🌍 **WAN connectivity**: instant state tracking

## Quick Setup

1. Install via HACS
2. Restart Home Assistant
3. Settings → Devices & Services → Add Integration → **OPNsense**
4. Enter your OPNsense IP, port, API key and secret

Full setup guide with required OPNsense privileges in the [README](https://github.com/spaghiari/ha-opnsense/blob/main/README.md).

## Security

This integration uses an **API key with minimal privileges** (read-only + firmware update). No SSH access required. No password storage. Your credentials stay on your Home Assistant instance.

## Tested Against

OPNsense 26.1.x · Home Assistant Core 2024.4+
