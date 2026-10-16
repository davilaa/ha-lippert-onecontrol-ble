# Lippert OneControl (BLE) for Home Assistant

Control a Lippert **OneControl** RV system (the "Compass Console" panel used with the
*Compass Connect* app) directly from Home Assistant over **Bluetooth LE**: no phone, no app,
no cloud.

> Not affiliated with or endorsed by Lippert Components. The protocol was worked out
> independently for interoperability. Use at your own risk, especially with slides and awnings.

## Supported devices

The integration discovers everything on the console's IDS-CAN bus and creates entities for
each device type it understands. Status layouts and commands follow the official app's own
device code.

| Device | Home Assistant entities | Status |
|---|---|---|
| Latching relays, type 2 (lights, water heaters, pumps, …) | Switch, current sensor | ✅ Verified |
| Slides & awnings (momentary H-bridge, type 2) | Cover with estimated position, travel-time / position / stall-current numbers, current sensor | ✅ Verified |
| Tank sensors | Level %; battery + signal quality for wireless senders | ✅ Verified (level) |
| Latching relays, type 1 (older modules) | Switch | 🧪 Experimental |
| Slides, awnings, doors, vent covers on type 1 H-bridges | Cover | 🧪 Experimental |
| Landing gear / stabilizer jacks | Cover, **disabled by default** (enable deliberately) | 🧪 Experimental |
| Dimmable lights | Light: brightness, Blink / Swell effects | 🧪 Experimental |
| RGB lights | Light: colour, Blink / Jump / Fade / Rainbow effects | 🧪 Experimental |
| RGBW lights | Light: hue / saturation / brightness, effects | 🧪 Experimental |
| HVAC zones (thermostats) | Climate: off / heat / cool / heat-cool / schedule, fan, gas vs heat-pump preset; outdoor temp sensor | 🧪 Experimental |
| Generator Genie | Switch (start / stop), state, battery, temperature | 🧪 Experimental |
| Generator on an H-bridge relay | Start / Stop buttons (held until the hour meter confirms) | 🧪 Experimental |
| Roof fans (e.g. MaxxFan) | Fan: speed, direction; lid as a cover | 🧪 Experimental |
| Hour meters | Hours (+ running / maintenance flags) | 🧪 Experimental |
| Temperature sensors | Temperature, sensor battery | 🧪 Experimental |
| Battery monitors | Voltage, current, state of charge | 🧪 Experimental |
| AC / DC power monitors | Voltage, current (+ charge, time remaining for DC) | 🧪 Experimental |
| Chassis info | Park brake, ignition, towable battery / brake voltage | 🧪 Experimental |
| Door locks | Lock state + battery (**read-only**) | 🧪 Experimental |

✅ = tested on real hardware. 🧪 = implemented from the app's code but not yet tested on a
real device — please open an issue with a debug log (see Troubleshooting) whether it works or not.

**Not supported:** levelers (the app drives them through a multi-screen guided state machine;
getting that wrong moves jacks), door lock *control* (uses a different, unverified write path),
and accessories that talk to the phone directly rather than through the console (TPMS, Mopeka
tank sensors, awning wind sensors).

Also on the device page: **Connection Status** and **Fault Status** (decoded fault codes),
**Pair / Re-pair Console**, **Emergency Stop** (halts every moving cover), **Clear All Faults**,
and **HA Console Control** (hand the console to the phone app and back).

Devices are found by their stable function name ("Ceiling Light", "Awning", …), not their bus
address, so entities survive the console being power-cycled.

## Compatibility

- Consoles that advertise over Bluetooth as **`LCIRemotec…`** (IDS-CAN gateway). Developed on
  a single console (part 26429-C). Reports from other models are welcome.
- **Not** for the WiFi "MyRvLink" OneControl gateways; those use a different protocol.

## Requirements

- **A Bluetooth adapter on the Home Assistant host**, within range of the console.
  The console requires a *bonded* (paired) connection, and **ESPHome Bluetooth proxies
  can't do bonded pairing**, so a proxy will not work. A USB dongle on a short USB extension
  cable (away from USB 3 ports) helps a lot with range.
- Home Assistant 2024.11 or newer.

## Installation

### HACS (recommended)
1. HACS → ⋮ → **Custom repositories** → add `https://github.com/davilaa/ha-lippert-onecontrol-ble`,
   category **Integration**.
2. Install **Lippert OneControl (BLE)** and restart Home Assistant.

### Manual
Copy `custom_components/compass_console` into your `<config>/custom_components/` folder and
restart Home Assistant.

## Setup

1. Settings → Devices & Services. The console is usually **auto-discovered**; otherwise
   **Add Integration** → *Lippert OneControl (BLE)* and enter the console's Bluetooth MAC.
2. **Pair once:** put the console in pairing mode (its *connect* button, light flashes), then
   press **Pair / Re-pair Console** on the device page and **accept the prompt on the console's
   screen**. Pairing windows are short, so press the button right after starting pairing mode.
3. Entities appear within a minute once the connection comes up.

## Using it alongside the console and the app

- **Physical panel:** works normally. After an HA command the integration holds the device for
  about 2 seconds (so rapid taps are instant), then explicitly hands control back.
- **Compass Connect app:** the console only accepts one Bluetooth connection at a time. Turn
  **HA Console Control** off before using the app and back on afterwards. The setting survives
  restarts, and the console keeps HA's pairing, so no re-pair is needed.

## Slides and awnings

These motors report no position, so the integration times the motor run.

- Set **Open Travel Time** / **Close Travel Time** per cover on the device page by timing a
  full run.
- If the estimate drifts, correct it with the **Position** number.
- Awnings stop automatically when motor current shows they've hit the end stop
  (**Stall Current**, default 7 A).
- Every move is capped at 120 s. **Emergency Stop** halts all moving covers.
- A cover that shows a fault won't move until you press **Clear All Faults**.

## Troubleshooting

- **Connects, then drops after ~2 seconds, every time:** the pairing was lost (for example
  another device paired, or the adapter changed). Re-run the pairing step.
- **Commands sometimes need a retry / "no seed" in debug logs:** Bluetooth range. Move the
  adapter closer or use an extension cable.
- **Debug logging:**
  ```yaml
  logger:
    logs:
      custom_components.compass_console: debug
  ```
  At debug level the integration also logs every device it sees on the bus, which is the
  most useful thing to include in an issue.

## How it works

Connect → KeySeed unlock → join the IDS-CAN bus as a node (address claim + presence
heartbeat) → open a short RemoteControl session with the target device → send the command.
See `custom_components/compass_console/compass_ble.py`; it also runs standalone:
`python compass_ble.py <MAC> list`.

## Development

`protocol.py` holds every status decoder and command encoder with no Bluetooth or Home
Assistant imports. Tests: `pip install pytest-homeassistant-custom-component` then `pytest`.
`tests/test_protocol.py` covers the byte formats (including real captures from a console);
`tests/test_ha.py` loads the integration into Home Assistant against a simulated bus.

## License

MIT, see [LICENSE](LICENSE).
