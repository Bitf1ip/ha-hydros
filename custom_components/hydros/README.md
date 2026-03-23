# HA-Hydros (Custom Integration)

<img src="logo.png" alt="Hydros DIY" width="300" />

## Summary
Custom Home Assistant integration for Hydros controllers. It connects to the Hydros cloud API to expose inputs, outputs, dosing history, and device health in Home Assistant.

![Example](dashboard.png)

This integration is strictly designed for monitoring.

⚠️ DO NOT rely on this integration's automations for life-critical functions (e.g temperature control, pumps) or when equipment/property damage can occur (e.g flood).

⚠️ This integration require internet to function and integrate with Hydros' cloud. Network issues will cause sensors to become unavailable (and automation to fail).

🛡️Leverage Hydros' own controller features for such functions as they have built-in resiliency for network & power outages and built-in safeguards.

Example of good usage for this integration includes: long term metrics, triggering alerts, automation to non life supporting 3rd party devices (e.g light, smart switch).

## Capabilities

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
- **Periodic refresh**:
  - Entity list refresh every 30 minutes to remove stale entities, while dosing log are pull every 5 minutes.

## Notes
- Credentials are stored in Home Assistant config entries.
- Debug samples are stored in memory (not persisted).

## ⚠️ Safety Warning & Disclaimer 

HA-Hydros is provided “as is” and “with all faults”, without warranty of any kind, express or implied. The author makes no representations or guarantees regarding safety, suitability, accuracy, reliability, availability, or fitness for any particular purpose.

This software is not designed, tested, or intended for safety-critical, life-supporting, or fail-safe control systems. Do not rely on this integration for life-critical functions (e.g. temperature control, circulation, oxygenation) or for scenarios where equipment failure could result in property damage (e.g. floods, electrical hazards, or fire).

Use of this software is entirely at your own risk. Improper configuration, software defects, network outages, cloud service changes, or unexpected behavior may result in equipment malfunction, property damage, or loss of aquatic life.

Always validate behavior in a controlled or non-critical environment before enabling automations. For critical functions, use Hydros’ native controller features, which are specifically designed with local control, redundancy, and safety safeguards.

In no event shall the author be liable for any direct, indirect, incidental, special, exemplary, or consequential damages arising from the use of, or inability to use, this software.

Nothing in this project constitutes professional, electrical, or safety advice.

This project is an independent, community-driven effort and is not affiliated with, authorized, maintained, or endorsed by CoralVue or Hydros. “Hydros” and “CoralVue” are trademarks of their respective owners and are used for identification purposes only.

## License

Licensed under MIT license
