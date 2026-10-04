# Changelog

All notable changes to this project are documented in this file.

## 0.4.0-beta - 2026-10-03

### Changed (breaking)
- Rebuilt on CoralVue's official [HYDROS Public API](https://www.coralvuehydros.com/api/) through the new `pyhydros2` library, replacing the unofficial Cognito/MQTT connection. Your Hydros username and password are no longer used: each device or collective needs a **provider key** and a **device key** (see "Getting your API keys" in the README).
- Existing installs ask you to re-authenticate. Enter a key pair for each collective you had configured, and entities and their history are moved over wherever a matching entity exists in the new API. Entities without an equivalent are left as they are and can be deleted.
- Device state refreshes every 7 seconds (it was MQTT push); dosing totals every 5 minutes.
- Requires Home Assistant 2026.8 or newer.
- The API keys field is masked, and re-authentication now also works when a key is revoked or replaced.

### Added
- Controls (need a read & write device key): On/Off/Auto selects for outputs, Auto/Off for dosers (Off suspends dosing), sliders for variable outputs with a **Resume Schedule** button, an operating-mode select, and buttons and number inputs for output commands such as a doser's manual dose, reverse dose and set reservoir.
- A **Running** binary sensor for each output.
- A **Mode Ends** sensor that shows when a timed mode such as Water Change ends.
- Per-controller health devices with bus voltage and current, temperature, boot time, last report, Wi-Fi and SD card status, and self-test counts.
- Output voltage, current, power and frequency sensors, dosing reservoir level, a Total Power sensor, an alerts summary, firmware version and collective status.
- The state-polling session is stored so restarts and reloads don't use up the API's limit of 5 new sessions per hour.

### Removed
- The Debug Sample and MQTT Health sensors, and the remote-control opt-in and disclaimer (controls now follow the permission of your device key).
- The periodic entity-list refresh.

### Known limitations
- The API reports no units, so analog input units are guessed from the firmware log type and the input name.
- Controller devices are named after their node ID, because the API doesn't return names for the controllers inside a collective. Use one device key per controller for friendly names, or rename them in Home Assistant.
- Collective and Wi-Fi/SD card status values are shown as raw numbers because their meanings are undocumented.
- If two outputs share an API key, their entities are told apart by name.

## 0.3.5 - 2026-07-31

### Fixed
- Pin a matched AWS IoT SDK dependency pair in the integration manifest (`awsiotsdk==1.30.0`, `awscrt==0.34.1`) to prevent native constructor/signature mismatches that can leave all entities unavailable on newer Python runtimes.
- Make MQTT subscription attempts non-fatal to entity/platform setup by scheduling subscriptions in the background instead of awaiting them during `async_added_to_hass`.

## 0.3.4 - 2026-05-24

### Added
- Add a new `XP8 Total Power` sensor sourced from MQTT health payloads (`health.*.acPower.powerI`), scaled by the existing `powerI` factor (`/10`) to report watts.

## 0.3.3 - 2026-05-22

### Added
- Support for HACS!

### Fixed
- Fix crash during mode-change failure recovery: `select.py` called `async_force_status_from_api` and `invalidate_collective_config` on `HydrosHub`, but neither method existed. When a mode change failed, the recovery path raised `AttributeError` before the original API error could be logged. Both methods are now implemented: `invalidate_collective_config` drops the stale cached config so the next read re-fetches from the cloud; `async_force_status_from_api` pulls authoritative status from the REST API, merges it into the status cache, and dispatches the per-thing signal so dependent entities refresh. (Ported from [JLay2026/ha-hydros@4d98d25](https://github.com/JLay2026/ha-hydros/commit/4d98d254f6ef6a1a30338f8984aef87f68475858) — credit to [@JLay2026](https://github.com/JLay2026).)

## 0.3.2 - 2026-05-08

### Added
- Support for Skimmer outputs on variable pumps (`type: o10vPump`, `family: vPump`).

### Fixed
- Normalize variable-pump `valueState` as a percentage by dividing by 100 (for example: `4500` -> `45.0%`).
- Prevent variable-pump `valueState` from being interpreted as binary on/off labels.

## 0.3.0 - 2026-04-06

### Fixed
- Add support for changing Hydros' mode. This requires to enable remote control under the integration's configuration (and to accept the risks).

## 0.2.0 - 2026-01-30

### Added
- Initial public custom integration release with config flow, sensors, and MQTT-backed status updates.
