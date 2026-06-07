# OPNsense for Home Assistant

[![GitHub Release][releases-shield]][releases]
[![License][license-shield]][license]
[![hacs][hacs-shield]][hacs]
[![Code style: black][black-shield]][black]

> 🇫🇷 **Version française : [README.fr.md](README.fr.md)**

A custom Home Assistant integration that exposes your **OPNsense firewall** as a native device with rich sensors, a real `update` entity for firmware management, and minimum-privilege API access.

![Integration screenshot](https://raw.githubusercontent.com/spaghiari/ha-opnsense/main/.github/screenshot.png)

---

## ✨ Features

### System monitoring
- **CPU load** - 1/5/15 min averages
- **RAM** - total, used, used %
- **Disk** - root partition usage with %
- **Uptime + last boot timestamp**
- **Hostname** + **CPU model**
- **OPNsense / FreeBSD / OpenSSL versions**

### WAN monitoring
- **Public IPv4 and IPv6** addresses
- **Real-time throughput** (in/out, in Mbps)
- **Total transferred** (in/out, in GB, `TOTAL_INCREASING` compatible with `utility_meter`)
- **Top 5 destinations** with reverse DNS resolution
- **WAN connectivity** binary sensor

### Firmware management
- **Native `update` entity** - compare installed vs latest version, one-click install
- **"Check for updates" button** to force a check on demand
- **`update_available` binary sensor** ready for automations

### Configuration
- **Setup via UI** - no YAML editing
- **Polling interval configurable** at runtime (30-600 seconds)
- **Multi-language UI** (English, French)

---

## 📋 Prerequisites

### Supported versions
- Home Assistant Core **2024.4.0** or newer
- OPNsense **26.1** or newer (older versions may work but are untested)

### OPNsense setup

You must create a **dedicated API user with minimal privileges** in OPNsense. Never use your `root` account.

#### 1. Create a group
Go to **System → Access → Groups → +** and create a group with these **8 privileges**:

| Privilege | Used for |
|---|---|
| `Lobby: Dashboard` | Base API access |
| `Diagnostics: ARP Table` | Network state |
| `Diagnostics: Show States` | State table |
| `Diagnostics: System Activity` | CPU/process info |
| `Status: Interfaces` | Interface details |
| `System: Firmware` | Firmware version + updates |
| `System: Status` | System information endpoint |
| `Reporting: Traffic` | WAN throughput sensors |

> ⚠️ **Do NOT grant "All pages"** - that would defeat the purpose of a restricted user.

#### 2. Create a user
**System → Access → Users → +**:
- Username: `homeassistant`
- Password: any random 32-character string (never used - just required by the form)
- Login shell: `Default (none for all but root)` - **no SSH access**
- Group membership: assign your newly created group

#### 3. Generate an API key
On the user list, click the **"key" icon** next to `homeassistant`. An `apikey.txt` file downloads automatically. **Open it once and save securely** - it contains:

```
key=...
secret=...
```

The secret is shown only on generation. Lose it = regenerate it.

---

## 🚀 Installation

### Option A - HACS (recommended)

1. In HACS, go to **Integrations → ⋮ → Custom repositories**
2. Add `https://github.com/spaghiari/ha-opnsense` as type `Integration`
3. Find **OPNsense** in the list and click **Download**
4. **Restart Home Assistant**

### Option B - Manual

1. Download the latest release ZIP from [releases][releases]
2. Extract `custom_components/opnsense_custom/` into your HA `config/custom_components/` folder
3. Final path should be `config/custom_components/opnsense_custom/__init__.py`
4. **Restart Home Assistant**

---

## ⚙️ Configuration

1. **Settings → Devices & Services → + Add Integration**
2. Search for **OPNsense**
3. Fill the form:
   - **Host**: your OPNsense IP (e.g. `192.168.1.1`)
   - **Port**: `443` (default HTTPS)
   - **API key**: the `key=` value from your `apikey.txt`
   - **API secret**: the `secret=` value
   - **Verify SSL certificate**: leave **unchecked** if using OPNsense's self-signed certificate (default)
4. Click **Submit**
5. **Pick the WAN interface** to monitor (throughput, public IP, connectivity).
   The integration pre-selects the auto-detected one - leave it on
   **Auto-detection** unless you run a non-standard / multi-WAN setup.

The integration tests the connection. On success, an **OPNsense** device appears with ~30 entities.

### Polling interval & WAN interface

Change the polling rate and the WAN interface at any time without reinstalling:

**Settings → Devices & Services → OPNsense → ⚙ Configure** → set the polling
interval (30-600 s) and the WAN interface.

### If your API key changes

Rotated or revoked the key in OPNsense? Home Assistant raises a
**re-authentication** prompt automatically - enter the new key/secret and the
integration reloads. You can also use **⋮ → Reconfigure** to change host/port.

---

## 🖥️ Ready-made dashboard (Glass NOC)

**Automatic** - on setup, the integration creates a polished **OPNsense**
dashboard in the sidebar: glassmorphism hero, status chips, circular RAM/disk/CPU
gauges, WAN throughput graph and top destinations. It is built from your **real
entity IDs**, so it works whatever your Home Assistant language is. Disable it
any time via **OPNsense → ⚙ Configure → Create an OPNsense dashboard in the
sidebar**.

### Frontend prerequisites (HACS Lovelace cards)

The default dashboard uses these custom cards - install them from **HACS →
Frontend** (one-time) for the intended look:

| Card | HACS name |
|---|---|
| Mushroom | `Mushroom` |
| ApexCharts Card | `apexcharts-card` |
| Mini Graph Card | `mini-graph-card` |
| card-mod | `card-mod` |
| Stack In Card | `stack-in-card` |

If a card is missing, the integration logs a warning (filter `opnsense_custom`)
and that card renders as "Custom element doesn't exist" - install it and reload.

> **Managed dashboard.** The layout is refreshed when the integration ships a
> new template version, so manual edits to it may be overwritten on upgrade. To
> customise freely, turn the option off and duplicate the dashboard, or start
> from [`dashboards/opnsense.yaml`](dashboards/opnsense.yaml).

---

## 📊 Entity list

### Enabled by default
- `sensor.opnsense_hostname`
- `sensor.opnsense_opnsense_version`
- `sensor.opnsense_cpu_load_1_min`
- `sensor.opnsense_ram_used_percent`
- `sensor.opnsense_disk_root_percent`
- `sensor.opnsense_uptime`
- `sensor.opnsense_last_boot`
- `sensor.opnsense_public_ipv4`
- `sensor.opnsense_wan_throughput_in/out`
- `sensor.opnsense_wan_total_received/transmitted`
- `sensor.opnsense_wan_top_destination_in/out`
- `binary_sensor.opnsense_update_available`
- `binary_sensor.opnsense_wan_connected`
- `update.opnsense_firmware`
- `button.opnsense_check_for_updates`

### Disabled by default (enable manually if needed)
- FreeBSD / OpenSSL versions
- CPU load 5/15 min
- Detailed disk sensors (total, used, available)
- RAM total
- WAN status (textual)
- CPU model
- Public IPv6
- Firmware installed/latest (textual)

---

## 💡 Usage examples

### Notification when update available

```yaml
alias: "OPNsense - Update notification"
trigger:
  - platform: state
    entity_id: binary_sensor.opnsense_update_available
    to: "on"
action:
  - service: notify.mobile_app_YOUR_PHONE
    data:
      title: "🛡️ OPNsense"
      message: "A firmware update is available."
```

### Monthly bandwidth tracking with utility_meter

```yaml
# configuration.yaml
utility_meter:
  wan_monthly_download:
    source: sensor.opnsense_wan_total_received
    name: "WAN Monthly Download"
    cycle: monthly
  wan_monthly_upload:
    source: sensor.opnsense_wan_total_transmitted
    name: "WAN Monthly Upload"
    cycle: monthly
```

### Top destinations card (Markdown)

```yaml
type: markdown
title: Top WAN destinations (download)
content: |
  {%- set top = state_attr('sensor.opnsense_wan_top_destination_in', 'top_5') %}
  {%- if top %}
  {%- for d in top %}
  **#{{ loop.index }}** - `{{ d.name }}` - **{{ d.rate_mbps }} Mbps**
  {% endfor %}
  {%- else %}
  *No significant traffic*
  {%- endif %}
```

---

## 🔒 Security

- The API user only has the **8 minimum privileges** listed above (read-only + firmware update)
- **No write access** to firewall rules, interfaces, or accounts
- **No shell, no SSH** for the API account
- Credentials are stored encrypted by Home Assistant
- If your API key leaks, **revoke and regenerate** from `System → Access → Users → ApiKeys`, then **Reconfigure** the integration with the new key

---

## 🐛 Troubleshooting

### "Cannot connect" error during setup
- Verify Home Assistant can reach OPNsense (`ping` from a HA terminal)
- Check the port (default 443)
- Test the API key manually:
  ```bash
  curl -sk -u "KEY:SECRET" https://<OPNSENSE_IP>/api/diagnostics/system/system_information
  ```

### Some sensors stay "Unavailable"
This usually means a **privilege is missing** on OPNsense. Check Home Assistant logs:

**Settings → System → Logs**, filter on `opnsense_custom`. You'll see lines like:
```
Échec de récupération de 'XXX': ... 403 ...
```
→ Add the corresponding privilege to the `homeassistant` group.

### "Insufficient privileges" error
Same as above - your API user doesn't have one of the 8 required privileges. Add the missing one and retry.

---

## 🤝 Contributing

PRs and issues welcome! Please:
- Open an issue first for bugs / feature requests
- Follow [black][black] code style for Python
- Test on your own OPNsense before submitting

### Reporting a bug
When filing a bug report, include:
- Home Assistant Core version
- OPNsense version
- Integration version (in `manifest.json`)
- Relevant logs (filter on `opnsense_custom`)

---

## ⚠️ Important notes

- **Always back up your OPNsense XML config** (System → Configuration → Backups) before triggering a firmware update from Home Assistant
- The integration uses **only documented API endpoints** - no SSH, no XML modification
- **Zenarmor metrics** are not yet supported (Zenarmor requires a separate API via Zenconsole, planned for a future companion integration)

---

## 📜 License

MIT - see [LICENSE](LICENSE).

This integration is **not affiliated with Deciso B.V.** (OPNsense vendor). "OPNsense" is a trademark of Deciso B.V.

---

## 🙏 Credits

- The OPNsense team for their robust REST API
- The Home Assistant community

---

[releases-shield]: https://img.shields.io/github/v/release/spaghiari/ha-opnsense?style=flat-square
[releases]: https://github.com/spaghiari/ha-opnsense/releases
[license-shield]: https://img.shields.io/github/license/spaghiari/ha-opnsense?style=flat-square
[license]: LICENSE
[hacs-shield]: https://img.shields.io/badge/HACS-Custom-orange.svg?style=flat-square
[hacs]: https://hacs.xyz
[black-shield]: https://img.shields.io/badge/code%20style-black-000000.svg?style=flat-square
[black]: https://github.com/psf/black
