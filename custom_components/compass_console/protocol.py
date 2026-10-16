"""IDS-CAN device status decoding and command encoding (no BLE / Home Assistant imports).

Layouts follow the OneControl app's own device classes. Every device command is an IDS-CAN
COMMAND message (type 130): the 29-bit ID's ext byte is the *command byte* and the payload is
the *data bytes* returned by the ``*_cmd`` helpers below as ``(command_byte, data)``.
All multi-byte fields are big-endian.
"""
from __future__ import annotations

# ---- DEVICE_TYPE ids -------------------------------------------------------------------------
DT_RELAY_1 = 3            # Latching Relay (type 1)
DT_HBRIDGE_1 = 6          # Momentary H-Bridge (type 1)
DT_TANK = 10
DT_HOUR_METER = 12
DT_RGB = 13
DT_HVAC = 16
DT_DIMMER = 20
DT_GENERATOR_GENIE = 24
DT_TEMPERATURE = 25
DT_AC_POWER = 26
DT_DC_POWER = 27
DT_RELAY_2 = 30           # Latching Relay (type 2)
DT_HBRIDGE_LATCH_2 = 32   # Latching H-Bridge (type 2) -- not created by the app; driven like 33
DT_HBRIDGE_2 = 33         # Momentary H-Bridge (type 2)
DT_FAN = 37
DT_CHASSIS = 39
DT_BATTERY = 49
DT_LOCK = 51
DT_RGBW = 61

# ---- FUNCTION_CLASS ids (see tables.FNCLASS) ------------------------------------------------------
FC_GENERATOR = 5
FC_LANDING_GEAR = 6
FC_VENT_COVER = 14
FC_STABILIZER = 24
FC_DOOR = 27

# ---- Device kinds this integration knows how to expose ----------------------------------------
K_RELAY = "relay"
K_COVER = "cover"
K_RELAY_GENERATOR = "relay_generator"   # generator start/stop wired through an H-bridge
K_TANK = "tank"
K_HOUR_METER = "hour_meter"
K_DIMMER = "dimmer"
K_RGB = "rgb"
K_RGBW = "rgbw"
K_HVAC = "hvac"
K_GENERATOR = "generator"                # Generator Genie (one-press start/stop)
K_TEMPERATURE = "temperature"
K_AC_POWER = "ac_power"
K_DC_POWER = "dc_power"
K_FAN = "fan"
K_CHASSIS = "chassis"
K_BATTERY = "battery"
K_LOCK = "lock"

_SIMPLE_KINDS = {
    DT_RELAY_1: K_RELAY, DT_RELAY_2: K_RELAY, DT_TANK: K_TANK, DT_HOUR_METER: K_HOUR_METER,
    DT_RGB: K_RGB, DT_HVAC: K_HVAC, DT_DIMMER: K_DIMMER, DT_GENERATOR_GENIE: K_GENERATOR,
    DT_TEMPERATURE: K_TEMPERATURE, DT_AC_POWER: K_AC_POWER, DT_DC_POWER: K_DC_POWER,
    DT_FAN: K_FAN, DT_CHASSIS: K_CHASSIS, DT_BATTERY: K_BATTERY, DT_LOCK: K_LOCK,
    DT_RGBW: K_RGBW,
}
HBRIDGE_TYPES = {DT_HBRIDGE_1, DT_HBRIDGE_LATCH_2, DT_HBRIDGE_2}


def device_kind(dtype: int, fclass: int) -> str | None:
    """Map a DEVICE_TYPE (+ FUNCTION_CLASS for h-bridges) to a kind, or None if unsupported."""
    if dtype in HBRIDGE_TYPES:
        return K_RELAY_GENERATOR if fclass == FC_GENERATOR else K_COVER
    return _SIMPLE_KINDS.get(dtype)


# ---- byte helpers ---------------------------------------------------------------------------------
def _u16(s, i):
    return (s[i] << 8) | s[i + 1]


def _s8x8(s, i):
    v = _u16(s, i)
    return (v - 0x10000 if v & 0x8000 else v) / 256.0


def _has(s, n):
    return s is not None and len(s) >= n


# ---- relays -------------------------------------------------------------------------------------
def relay_is_on(dtype, s):
    if not _has(s, 1):
        return False
    if dtype == DT_RELAY_1:
        return bool(s[0] & 0x01)
    return (s[0] & 0x0F) == 1


def relay_faulted(dtype, s):
    """Output latched off by a fault (needs a 'clear fault' command)."""
    if not _has(s, 1):
        return False
    if dtype in (DT_RELAY_1, DT_HBRIDGE_1):
        return bool(s[0] & 0x40)
    return bool(s[0] & 0x20)


def relay_current(dtype, s):
    """Load current in amps (type-2 relays / h-bridges only), else None."""
    if dtype in (DT_RELAY_1, DT_HBRIDGE_1) or not _has(s, 4):
        return None
    raw = _u16(s, 2)
    return None if raw == 0xFFFF else raw / 256.0


def relay_dtc(dtype, s):
    if dtype in (DT_RELAY_1, DT_HBRIDGE_1) or not _has(s, 6):
        return 0
    return _u16(s, 4)


def relay_cmd(dtype, on):
    if dtype == DT_RELAY_1:
        # latch bit 0x80 | disconnect-state 0x02 | state 0x01 (the app sets both from `on`)
        return 0, bytes([0x83 if on else 0x80])
    return (1 if on else 0), b""


def clear_fault_cmd(dtype):
    if dtype in (DT_RELAY_1, DT_HBRIDGE_1):
        return 0, bytes([0x40])
    return 3, b""


# ---- h-bridges (slides, awnings, ...) ---------------------------------------------------------
MOVE_STOP, MOVE_FORWARD, MOVE_REVERSE = 0, 1, 2


def _hbridge1_relays(direction, fclass, fname):
    """Type-1 h-bridges energize relay 1 or 2; which one means 'forward' depends on the
    function class (mirrors the app's RelayHBridgeEnergizedExtension)."""
    if direction == MOVE_STOP:
        return 0x00
    swapped = fclass in (FC_STABILIZER, FC_GENERATOR, FC_LANDING_GEAR, FC_VENT_COVER) or (
        fclass == FC_DOOR and "ramp" in (fname or "").lower())
    forward_is_relay1 = not swapped
    relay1 = (direction == MOVE_FORWARD) == forward_is_relay1
    return 0x01 if relay1 else 0x04


def move_cmd(dtype, direction, fclass=0, fname=""):
    if dtype == DT_HBRIDGE_1:
        return 0, bytes([_hbridge1_relays(direction, fclass, fname)])
    return direction, b""


def hbridge_moving(dtype, s):
    if not _has(s, 1):
        return False
    if dtype == DT_HBRIDGE_1:
        return bool(s[0] & 0x03)
    return (s[0] & 0x0F) in (1, 2)


def hbridge_position(dtype, s):
    if dtype == DT_HBRIDGE_1 or not _has(s, 2):
        return None
    return s[1] if s[1] <= 100 else None


# ---- tanks --------------------------------------------------------------------------------------
def tank_level(s):
    if not _has(s, 1):
        return None
    return min(s[0] & 0x7F, 100)


def tank_battery(s):
    return s[1] if _has(s, 8) and s[1] <= 100 else None


def tank_quality(s):
    return s[2] if _has(s, 8) and s[2] <= 100 else None


# ---- hour meter -----------------------------------------------------------------------------------
def hour_meter(s):
    if not _has(s, 5):
        return None
    flags = s[4]
    return {
        "valid": not flags & 0x80,
        "hours": ((s[0] << 24) | (s[1] << 16) | (s[2] << 8) | s[3]) / 3600.0,
        "running": bool(flags & 0x01),
        "maintenance_due": bool(flags & 0x02),
        "maintenance_past_due": bool(flags & 0x04),
        "stopping": bool(flags & 0x08),
        "starting": bool(flags & 0x10),
        "error": bool(flags & 0x20),
    }


# ---- dimmable light ------------------------------------------------------------------------------
DIM_OFF, DIM_ON, DIM_BLINK, DIM_SWELL, DIM_RESTORE = 0, 1, 2, 3, 127
DIM_DEFAULT_CYCLE = 220


def dimmer(s):
    if not _has(s, 8):
        return None
    return {
        "mode": s[0], "on": s[0] > 0, "max_brightness": s[1], "duration": s[2],
        "brightness": s[3], "cycle1": _u16(s, 4), "cycle2": _u16(s, 6),
    }


def dimmer_cmd(mode, max_brightness=0, duration=0, cycle1=0, cycle2=0):
    if mode in (DIM_BLINK, DIM_SWELL) and (not cycle1 or not cycle2):
        cycle1 = cycle2 = DIM_DEFAULT_CYCLE
    return 0, bytes([mode & 0xFF, max_brightness & 0xFF, duration & 0xFF,
                     (cycle1 >> 8) & 0xFF, cycle1 & 0xFF, (cycle2 >> 8) & 0xFF, cycle2 & 0xFF, 0])


# ---- RGB / RGBW lights ---------------------------------------------------------------------------
RGB_OFF, RGB_ON, RGB_BLINK, RGB_RESTORE = 0, 1, 2, 127
RGB_EFFECTS = {"Jump 3": 4, "Jump 7": 5, "Fade 3": 6, "Fade 7": 7, "Rainbow": 8}
RGB_DEFAULT_INTERVAL = 750    # the app's "slow" transition interval (ms)
RGB_BLINK_INTERVAL = 207      # the app's "slow" blink on/off time


def rgb(s):
    if not _has(s, 8):
        return None
    return {"mode": s[0], "on": s[0] > 0, "rgb": (s[1], s[2], s[3]),
            "duration": s[4], "interval": _u16(s, 5)}


def rgb_cmd(mode, color=(0, 0, 0), duration=0, interval=0):
    return 0, bytes([mode & 0xFF, color[0] & 0xFF, color[1] & 0xFF, color[2] & 0xFF,
                     duration & 0xFF, (interval >> 8) & 0xFF, interval & 0xFF, 0])


def rgbw(s):
    """RGBW lights report HSV packed into 24 bits: hue 0-359 (9 bits), sat 0-100, value 0-100."""
    if not _has(s, 8):
        return None
    hsv = (s[1] << 16) | (s[2] << 8) | s[3]
    return {"mode": s[0], "on": s[0] > 0, "hue": (hsv >> 15) & 0x1FF,
            "saturation": (hsv >> 8) & 0x7F, "value": hsv & 0x7F,
            "duration": s[4], "interval": _u16(s, 5)}


def rgbw_cmd(mode, hue=0, saturation=0, value=0, duration=0, interval=0):
    hsv = (min(int(value), 100) & 0x7F) | ((min(int(saturation), 100) & 0x7F) << 8) | (
        (int(hue) % 360 & 0x1FF) << 15)
    return 0, bytes([mode & 0xFF, (hsv >> 16) & 0xFF, (hsv >> 8) & 0xFF, hsv & 0xFF,
                     duration & 0xFF, (interval >> 8) & 0xFF, interval & 0xFF, 0])


# ---- HVAC climate zone --------------------------------------------------------------------------
HVAC_OFF, HVAC_HEAT, HVAC_COOL, HVAC_BOTH, HVAC_SCHEDULE = 0, 1, 2, 3, 4
SRC_GAS, SRC_HEAT_PUMP, SRC_OTHER = 0, 1, 2
FAN_AUTO, FAN_HIGH, FAN_LOW = 0, 1, 2
HVAC_CAP_GAS, HVAC_CAP_AC, HVAC_CAP_HEAT_PUMP, HVAC_CAP_MULTI_FAN = 1, 2, 4, 8
ZONE_STATUS = {
    0: "off", 1: "idle", 2: "cooling", 3: "heating_heat_pump", 4: "heating_electric",
    5: "heating_gas", 6: "heating_gas_override", 7: "dead_time", 8: "load_shedding",
}
_TEMP_INVALID = (0x8000, 0x2FF0)


def _temp_f(s, i):
    if _u16(s, i) in _TEMP_INVALID:
        return None
    t = _s8x8(s, i)
    return t if -40.0 <= t <= 140.0 else None


def hvac(s):
    if not _has(s, 8):
        return None
    zone = s[3] & 0x8F
    return {
        "heat_mode": s[0] & 0x07, "heat_source": (s[0] >> 4) & 0x03, "fan_mode": (s[0] >> 6) & 0x03,
        "low_trip_f": s[1], "high_trip_f": s[2],
        "zone_status": ZONE_STATUS.get(zone & 0x0F, "off") if not zone & 0x80 else "failed",
        "failed_thermistor": bool(s[3] & 0x80),
        "indoor_f": _temp_f(s, 4), "outdoor_f": _temp_f(s, 6),
    }


def hvac_cmd(heat_mode, heat_source, fan_mode, low_trip_f, high_trip_f):
    command = (heat_mode & 0x07) | ((heat_source & 0x03) << 4) | ((fan_mode & 0x03) << 6)
    return 0, bytes([command, int(low_trip_f) & 0xFF, int(high_trip_f) & 0xFF])


# ---- Generator Genie -------------------------------------------------------------------------------
GEN_STATES = {0: "off", 1: "priming", 2: "starting", 3: "running", 4: "stopping"}


def generator(s):
    if not _has(s, 5):
        return None
    t_raw = _u16(s, 3)
    temp = None if t_raw in (0x8000, 0x7FFF) else _s8x8(s, 3)
    return {
        "state": GEN_STATES.get(s[0] & 0x07, "unknown"), "quiet_hours": bool(s[0] & 0x80),
        "battery_v": _u16(s, 1) / 256.0, "temperature_f": temp,
        "dtc": _u16(s, 5) if _has(s, 7) else 0,
    }


def generator_cmd(on):
    return 0, bytes([2 if on else 1])


# ---- temperature sensor ---------------------------------------------------------------------------
def temperature(s):
    if not _has(s, 8):
        return None
    return {
        "temperature_c": None if _u16(s, 0) == 0x7FFF else _s8x8(s, 0),
        "battery_v": s[2] / 64.0, "battery_pct": s[3] if s[3] <= 100 else None,
        "low_battery": bool(s[4] & 0x80), "high_alert": bool(s[5] & 0x80),
        "low_alert": bool(s[6] & 0x80),
    }


# ---- power monitors -----------------------------------------------------------------------------
def _u16x16(s, i):
    # The app reads 4 bytes from the field start (fields overlap); zero-pad past the end.
    b = bytes(s[i:i + 4]).ljust(4, b"\x00")
    return int.from_bytes(b, "big") / 65536.0


def ac_power(s):
    if not _has(s, 5):
        return None
    return {
        "voltage": None if _u16(s, 0) == 0xFFFF else round(_u16x16(s, 0), 2),
        "current": None if _u16(s, 2) == 0xFFFF else round(_u16x16(s, 2), 2),
        "quality": s[4] & 0x0F,
    }


def dc_power(s):
    if not _has(s, 7):
        return None
    soc = s[4] & 0x7F
    mins = _u16(s, 5)
    return {
        "voltage": None if _u16(s, 0) == 0xFFFF else round(_u16x16(s, 0), 2),
        "current": None if _u16(s, 2) == 0xFFFF else round(_u16x16(s, 2), 2),
        "soc": soc if soc <= 100 else None, "charging": bool(s[4] & 0x80),
        "minutes_remaining": None if mins == 0xFFFF else mins,
    }


# ---- battery monitor -----------------------------------------------------------------------------
def battery(s):
    if not _has(s, 8):
        return None
    cur = (s[2] << 16) | (s[3] << 8) | s[4]
    if cur & 0x800000:
        cur -= 0x1000000
    soc = s[5]
    if soc <= 100:
        soc_v = soc
    elif 128 <= soc <= 228:
        soc_v = soc & 0x7F   # estimate, still stabilizing
    else:
        soc_v = None         # 254 invalid / 255 computing
    return {
        "voltage": _s8x8(s, 0), "current": cur / 256.0, "soc": soc_v,
        "low_soc_alert": bool(s[6] & 0x80), "idle_alert": bool(s[7] & 0x80),
    }


# ---- fan / vent (e.g. MaxxFan) ---------------------------------------------------------------------
FAN_CMD_SPEED, FAN_CMD_VENT, FAN_CMD_RESTORE = 0, 1, 2


def fan(s):
    if not _has(s, 1):
        return None
    speed = s[1] - 256 if _has(s, 2) and s[1] > 127 else (s[1] if _has(s, 2) else 0)
    return {
        "rain_detected": s[0] == 0,
        "speed": abs(speed), "direction": "reverse" if speed < 0 else "forward",
        "vent": s[2] if _has(s, 3) else None,
    }


def fan_speed_cmd(speed, reverse=False):
    speed = max(0, min(100, int(speed)))
    v = -speed if reverse else speed
    return FAN_CMD_SPEED, bytes([v & 0xFF])


def fan_vent_cmd(position):
    # The vent cover only knows 0 / 25 / 50 / 75 / 100.
    position = min((0, 25, 50, 75, 100), key=lambda p: abs(p - int(position)))
    return FAN_CMD_VENT, bytes([position])


# ---- chassis info -----------------------------------------------------------------------------------
_PARK = {1: "released", 2: "engaged"}
_IGN = {1: "on", 2: "off"}


def chassis(s):
    if not _has(s, 1):
        return None
    return {
        "park_brake": _PARK.get(s[0] & 0x03),
        "ignition": _IGN.get((s[0] >> 2) & 0x03),
        "towable_battery_v": (s[2] / 16.0) if _has(s, 3) and s[2] != 0xFF else None,
        "towable_brake_v": (s[3] / 16.0) if _has(s, 4) and s[3] != 0xFF else None,
    }


# ---- door lock (status only) -----------------------------------------------------------------------
_LOCK = {1: "unlocked", 2: "locked"}


def lock(s):
    if not _has(s, 4):
        return None
    return {
        "state": _LOCK.get(s[0] & 0x0F), "control_disabled": bool(s[0] & 0x80),
        "battery": s[1], "low_battery": bool(s[2] & 0x80), "health": s[3],
    }
