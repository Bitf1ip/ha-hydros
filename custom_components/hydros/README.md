# HA-Hydros (Custom Integration)

## Summary
Custom Home Assistant integration for Hydros controllers. It connects to the Hydros cloud API to expose inputs, outputs, dosing history, and device health in Home Assistant.

## Capabilities
Current focus is on exposing Hydros sensors to home assistant, although controlling Hydros is technically possible, it is not currently supported. 

⚠️ DO NOT rely on this integration's automations for life-critical functions (e.g temperature control, pumps) or when equipment/property damage can occur (e.g flood). 

🛡️DO leverage Hydros' own controller features for such functions as they have built-in resiliency for network & power outages and built-in safeguards.

Example of good usage for this integration includes: long term metrics, triggering alerts, automation to non life supporting 3rd party devices (e.g light, smart switch).

- **Config flow**: Username/password login and collective or standalone selection.
- **Sensors**:
  - Hydros inputs (temp, probe, triple-level, etc.) with units and transforms.
  - Output measurements (power, voltage, current, frequency, reservoir where present).
  - Doser totals (**Dosed Today**) from the Hydros logs API.
  - Collective health (MQTT online/offline) and current mode.
  - Collective alerts summary sensor (aggregates per-sensor alerts).
  - Debug sample sensor (stores latest S3 config + MQTT payload snapshot).
- **Binary sensors**:
  - Binary outputs (e.g., relays/outlets).
  - Rope leak inputs as binary sensors.
- **Buttons**:
  - Debug Sample button (collects one S3 config snapshot + one MQTT payload snapshot).
- **MQTT**:
  - Subscribes to AWS IoT MQTT for real-time updates.
  - Auto retry + reconnect logic on disconnect.
- **Periodic refresh**:
  - Entity list refresh every 30 minutes to remove stale entities.

## Notes
- Credentials are stored in Home Assistant config entries.
- Debug samples are stored in memory (not persisted).

## ⚠️ Safety Warning & Disclaimer 

HA-Hydros is provided "as is" and "with all faults." The author makes no representations or warranties regarding safety, suitability, accuracy, or reliability.

Use at your own risk. Improper configuration or software bugs could lead to equipment malfunction or fire, property damage (e.g., floods), or loss of aquatic life.

Always test new configurations in a dry-run or controlled environment. This project is an independent community effort and is not affiliated with, authorized, maintained, or endorsed by CoralVue Hydros.
