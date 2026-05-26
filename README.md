# Terma Smart — Home Assistant integration

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz/)
[![Version](https://img.shields.io/badge/version-1.0.0-blue.svg)](custom_components/terma/manifest.json)
[![IoT Class](https://img.shields.io/badge/iot--class-local__polling-green.svg)](https://developers.home-assistant.io/docs/creating_integration_manifest#iot-class)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Repo: <https://github.com/vanyasvl/terma-smart-local>

Local-network control for **Terma Smart** electric towel-rail thermostats
(`com.termaconnect.mobile` v1.32 firmware family). Reverse-engineered from
the official Android app — no cloud account, no third-party services at
runtime. Your thermostat talks JSON over TCP on the same Wi-Fi as Home
Assistant and that's it.

## Highlights

- **Climate entity** with HEAT / AUTO (schedule) / OFF modes, native target
  temperature slider, and live heating-action status.
- **Zeroconf auto-discovery** — devices announcing `_terma._tcp.local` are
  offered for setup automatically.
- **Local-only** — `iot_class: local_polling`. The integration never reaches
  out to Terma's cloud.
- **One config entry, many devices** — add as many thermostats as you have on
  the LAN under a single integration entry.
- **Rich state** — separate sensors for room/heater temperature, humidity,
  energy usage, heating level; switches for power / dryer boost / parental
  lock; binary sensor for the heating element; configurable heating-level
  number entity.

## Requirements

- Home Assistant **2024.1** or newer.
- The thermostat and HA must be on the same LAN (the device listens on TCP
  port 5005).
- The serial number of each thermostat (printed on the device label, format
  e.g. `HBKB693DAP#1#D1`). Auto-discovery fills this in for you.

## Installation

### HACS (recommended)

1. In HACS → **Integrations** → ⋮ menu → **Custom repositories**.
2. Add `https://github.com/vanyasvl/terma-smart-local` with category **Integration**.
3. Install **Terma Smart**, then restart Home Assistant.
4. **Settings → Devices & Services → Add Integration → "Terma Smart"** — or
   accept the discovery card that pops up if your thermostat is on the same
   network.

### Manual

Copy `custom_components/terma/` from this repo into your Home Assistant
config directory so the path becomes `<config>/custom_components/terma/`.
Restart Home Assistant and add the integration as above.

## Configuration

There's no YAML — everything is configured through the UI.

| Field | Description |
|---|---|
| **Host** | The thermostat's LAN IP. Pre-filled by zeroconf when applicable. |
| **Serial** | The device serial (`HBKBxxxxxxx#n#Dn`). Pre-filled by zeroconf. |
| **Port** | Defaults to **5005**. Don't change unless you've remapped it. |
| **Name** | Optional friendly name for the device in HA. |

Polling interval is fixed at **30 seconds** (see `const.py`).

## Entities per device

| Platform | Entity | Source field |
|---|---|---|
| `climate.*` | Thermostat (HVAC modes + target + room temp) | `isEnabled`, `manualMode`, `temperature`, `isHeating`, `schedule` |
| `sensor.*_room_temperature` | Room temperature (°C) | `temperature` (Kelvin) |
| `sensor.*_heater_temperature` | Heating element temperature (°C) | `heaterTemperature` |
| `sensor.*_humidity` | Humidity (%) — disabled by default | `humidity` |
| `sensor.*_power_usage` | Cumulative energy (Wh) | `powerUsage` |
| `sensor.*_heating_level` | Heating level (%) | `heatingCoefficient` |
| `switch.*_power` | Power on/off | `isEnabled` |
| `switch.*_dry` | Dryer (towel-boost) toggle | `isDryerOn` |
| `switch.*_parental_lock` | Child lock | `isParentalControlOn` |
| `binary_sensor.*_heating` | Element actively heating | `isHeating` |
| `number.*_heating_level` | Heating-level setpoint (0–100 %) | `heatingCoefficient` |

The `climate` entity and the `power` switch share the same underlying
`isEnabled` state — toggling either is reflected in the other on the next
poll. The `binary_sensor.heating` and the climate's `hvac_action` likewise
mirror `isHeating`. They're kept separate so you can wire each piece into
automations / dashboards independently.

## How target temperature works (important)

The Terma protocol is **asymmetric**: the manual setpoint (`setTemperature`)
and rated heating power (`powerCapabilities`) can be *written* to the
device but the device **does not echo them back** in its telemetry. The
integration handles this two ways:

- **While in HEAT (`manualMode ∈ {0,1,2}`)**: the integration caches the
  value last written and surfaces it as `target_temperature`. The cache is
  persisted across HA restarts via `RestoreEntity`.
- **While in AUTO (`manualMode == 3`)**: the integration decodes the
  device's weekly `schedule` field and returns the active entry's setpoint.

Caveat: if you change the setpoint from the physical thermostat while HA is
offline, the restored cache is stale. Nudging the slider in HA reconciles
it. There's no way around this — the firmware doesn't expose a read-back
for the manual setpoint.

See `PROTOCOL.md` for the full reverse-engineered protocol reference.

## CLI tool

The repo ships `terma_cli.py` — a stand-alone Python CLI that exercises the
same protocol (LAN + optional cloud API) from the command line. Useful for
troubleshooting and bulk operations.

```bash
./terma_cli.py --host 192.168.1.42 --serial 'HBKB693DAP#1#D1' status
./terma_cli.py --host 192.168.1.42 --serial 'HBKB693DAP#1#D1' temp 21.5
./terma_cli.py discover
```

It needs no extra dependencies beyond Python 3.10+.

## Limitations

- **Pre-stable schema**: the config entry layout may change in early
  versions. There is no `async_migrate_entry` yet — if a future release
  breaks compatibility, remove the integration and re-add it.
- **No firmware update**: the integration does not perform OTA updates.
- **Don't probe unknown action IDs**: doing so will brick the device into
  pairing mode. The integration sticks strictly to the action IDs in
  `PROTOCOL.md`.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Device discovered but won't set up | Confirm port 5005 is reachable from HA: `nc -zv <ip> 5005`. |
| Climate target shows the schedule value when device is in HEAT | Cache wasn't restored (e.g., HA fresh install). Set a target once via HA; it'll persist thereafter. |
| State updates feel slow | 30 s polling is fixed. Set targets via HA's slider — writes trigger an immediate refresh. |
| Device drops into pairing mode (blinking +/−) | Some external tool sent it an unsupported action ID. Re-pair and avoid raw probing. |

## Credits & disclaimers

- Protocol reverse-engineered from the official Terma Connect Android app
  (jadx + Wireshark). All field names and encodings used here are sourced
  from those decompiled artifacts; cross-referenced packet captures are
  included in the repo for transparency.
- **Not affiliated with or endorsed by Terma sp. z o.o.** Trademarks belong
  to their owners.
- Provided as-is; use at your own risk. The author bears no liability for
  damage, comfort loss, or runaway towel-warming incidents.

## License

[MIT](LICENSE) © 2026 Ivan Semenov
