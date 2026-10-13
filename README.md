# Amina S — ZHA custom quirk

A [ZHA](https://www.home-assistant.io/integrations/zha/) custom device handler
("quirk") for the **Amina S** EV charger by Amina Distribution AS.

Out of the box, ZHA recognises the Amina S as little more than a dimmable
light: you can arm/disarm it (on/off) and nudge the charging current via the
level-control cluster. This quirk teaches ZHA about the charger's
manufacturer-specific behaviour so Home Assistant exposes proper entities for
charging status, alarms, energy counters, single-phase control and offline
behaviour.

It is based on the official *"Zigbee in Amina S"* specification (Revision E)
and learns from the community
[Zigbee2MQTT converter by somlioy](https://github.com/somlioy/amina_s).

> **Status / disclaimer.** This is an unofficial, community quirk. It has been
> written carefully against the spec but is provided as-is. I have tested it only
> in a single-phase setup.

---

## What you get

After installing the quirk and reconfiguring the device, the Amina S exposes
the following entities in Home Assistant (in addition to the standard ZHA
on/off switch and the electrical-measurement sensors for voltage, current,
power and frequency that ZHA already provides):

### Controls

| Entity | Type | Description |
| --- | --- | --- |
| **Charge current limit** | Number (6–32 A) | Maximum amperage the EV may draw. Writes are clamped to the device's reported hardware limit. |
| **Force single-phase charging** | Switch | Restrict charging to a single phase. A charging restart is required for the change to take effect. |
| **Enable offline mode** | Switch | Apply offline behaviour when the Zigbee connection is lost. |
| **Offline: single-phase** | Switch | Use single-phase charging while offline. |
| **Offline current limit** | Number (6–32 A) | Current to draw while the charger is offline. |
| **Time to offline** | Number (0–60 s) | Delay before offline behaviour kicks in after connection loss. |

### Status sensors

| Entity | Type | Description |
| --- | --- | --- |
| **EV status** | Sensor (text) | Human-readable charging state: *Not connected / EV connected / Ready to charge / Charging / Paused*, with a *(derated)* suffix when thermally throttling. |
| **EV connected** | Binary sensor (`plug`) | An EV is plugged in. |
| **Charging** | Binary sensor (`battery_charging`) | Power is actively being delivered. |
| **EV ready** | Binary sensor | The EV is ready to accept charge. |
| **Charging paused** | Binary sensor | Charging is currently paused. |
| **Thermal derating** | Binary sensor (`heat`) | The charger is derating due to high temperature. |

### Diagnostics

| Entity | Type | Description |
| --- | --- | --- |
| **Alarms** | Sensor (text) | Comma-separated list of active alarms, or *No alarm*. |
| **Alarm active** | Binary sensor (`problem`) | Any alarm bit is set. |
| **Hardware current limit** | Sensor (A) | The charger's actual `MaxLevel`, set by the physical current-selector wheel. |

### Energy

| Entity | Type | Description |
| --- | --- | --- |
| **Total energy** | Sensor (kWh, `total_increasing`) | Lifetime energy consumed by the charger. Suitable for the HA Energy dashboard. |
| **Last session energy** | Sensor (kWh, `total`) | Energy used in the most recent charging session. |

---

## Requirements

- Home Assistant with the **ZHA** integration and a working Zigbee coordinator.
- The Amina S already paired to your ZHA network. (If it isn't yet, pair it
  first — using the install code printed on the device helps. The quirk only
  changes how an already-known device is interpreted.)

---

## Installation

1. **Create a directory** for custom quirks on your Home Assistant host, for
   example:

   ```
   /config/custom_zha_quirks/
   ```

2. **Copy `amina_s.py`** into that directory:

   ```
   /config/custom_zha_quirks/amina_s.py
   ```

3. **Point ZHA at the directory** in `configuration.yaml`:

   ```yaml
   zha:
     custom_quirks_path: /config/custom_zha_quirks/
   ```

4. **Restart Home Assistant** (Developer Tools → restart, or
   Settings → System → Restart).

5. **Reconfigure the device** so ZHA re-reads its clusters and creates the new
   entities:

   - Go to **Settings → Devices & Services → ZHA**.
   - Open the Amina S device.
   - Click the **⋮ menu → Reconfigure device**.

   If reconfigure doesn't surface everything, removing and re-pairing the
   device will always pick up the quirk cleanly.

> **Tip.** Some of the derived entities (EV status, alarms, etc.) only become
> available once the charger first reports its state. The quirk schedules a
> read shortly after start-up and on reconfigure, but you may briefly see
> *unknown* until the first report arrives.

---

## Verifying it loaded

- On the Amina S device page in ZHA, the **device signature / Zigbee info**
  section should show the custom *Amina S Control* cluster (`0xFEE7`) and the
  new entities should appear under the device.
- To debug, enable verbose logging in `configuration.yaml` and restart:

  ```yaml
  logger:
    default: info
    logs:
      homeassistant.components.zha: debug
      zigpy: debug
      zigpy.quirks: debug
      zigpy.zcl: debug
  ```

  Then check **Settings → System → Logs** (or `/config/home-assistant.log`)
  for entries mentioning `amina`, `zigpy.quirks` or `QuirkBuilder`. A quirk
  with a syntax/import error will simply fail to load and log the traceback
  here.

---

## How it works (technical notes)

The quirk targets **endpoint 10** (the charger-control endpoint) and replaces
three clusters:

- **`AminaControlCluster` (0xFEE7)** — the manufacturer-specific cluster. It
  defines the documented attributes (alarms, EV status, connect status,
  single-phase, offline settings, energy counters) and pins the Amina
  manufacturer code `0x143B` so every read/write sets the
  manufacturer-specific frame bit. It also derives a set of "virtual"
  attributes from the `ev_status` and `alarms` bitmaps so each status/alarm
  can be surfaced as its own Home Assistant entity. Reporting is configured
  and an initial read is performed on bind, with an additional delayed read on
  start-up so entities recover after a plain Home Assistant restart.

- **`AminaLevelControl` (Level Control)** — translates writes to
  `current_level` into `moveToLevelWithOnOff` commands, which is how the spec
  says charging current should be changed. Writes are clamped to the device's
  reported `MinLevel`/`MaxLevel` (with a 6–32 A fallback) so values above the
  physical current-selector limit never reach the charger.

- **`AminaOnOff` (On/Off)** — intercepts the **Toggle** command, which the
  spec warns "should not be used as it may result in unwanted results," and
  rewrites it into an explicit On or Off based on the cached state (defaulting
  to Off when the state is unknown).

### Known limitations

- The **Charge current limit** slider always offers up to 32 A in the UI.
  QuirkBuilder cannot bind a number entity's range to a live device attribute,
  so the real hardware cap is enforced on write (clamping) and surfaced
  separately via the *Hardware current limit* sensor rather than by shrinking
  the slider.
- The charger does not (yet) support attribute **reporting** for the energy
  counters, so *Total energy* and *Last session energy* update via Home
  Assistant's periodic polling rather than instantly.
- The Toggle rewrite only applies to commands sent **through this quirk**. A
  toggle sent to the charger directly by another bound Zigbee device cannot be
  intercepted here.
- Future model identifiers (`S1`, `S3`, `C1`, `C3`) are registered
  speculatively per the spec; if Amina ships a different string the
  `applies_to(...)` list in `amina_s.py` may need updating.

---

## Credits

- Amina Distribution AS — *"Zigbee in Amina S"* specification.
- [somlioy/amina_s](https://github.com/somlioy/amina_s) — the Zigbee2MQTT
  converter that informed the attribute map and status decoding.
- [zigpy/zha-device-handlers](https://github.com/zigpy/zha-device-handlers) —
  the ZHA quirks framework and documentation.
