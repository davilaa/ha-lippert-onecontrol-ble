"""Decoder / encoder tests. Fixtures marked "live" are real status bytes from a console."""
import importlib.util
import pathlib
import sys
import types

import pytest

PKG = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "compass_console"
sys.path.insert(0, str(PKG))
sys.modules.setdefault("bleak", types.SimpleNamespace(BleakClient=object))  # not needed here

import protocol as P  # noqa: E402
from tables import FNCLASS  # noqa: E402

_spec = importlib.util.spec_from_file_location("compass_ble", PKG / "compass_ble.py")
compass_ble = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(compass_ble)
Device = compass_ble.Device

H = bytes.fromhex


# ---- device classification ------------------------------------------------------------------
def test_function_class_table():
    assert len(FNCLASS) == 460
    assert FNCLASS[105] == 2      # Awning -> AWNING
    assert FNCLASS[96] == 10      # Slide -> SLIDE
    assert FNCLASS[61] == 8       # Ceiling Light -> LIGHT


@pytest.mark.parametrize("dtype,func,kind", [
    (30, 61, P.K_RELAY), (3, 61, P.K_RELAY), (33, 105, P.K_COVER), (6, 96, P.K_COVER),
    (10, 67, P.K_TANK), (20, 7, P.K_DIMMER), (13, 7, P.K_RGB), (61, 7, P.K_RGBW),
    (16, 0, P.K_HVAC), (24, 0, P.K_GENERATOR), (37, 0, P.K_FAN), (43, 320, None),
])
def test_kinds(dtype, func, kind):
    assert P.device_kind(dtype, FNCLASS[func]) == kind


def test_generator_hbridge_is_not_a_cover():
    gen_func = FNCLASS.index(P.FC_GENERATOR)
    assert P.device_kind(33, P.FC_GENERATOR) == P.K_RELAY_GENERATOR
    d = Device(0x10, 33, gen_func, "00" * 6, 0, H("c0ff00000000"))
    assert not d.is_cover and d.kind == P.K_RELAY_GENERATOR


# ---- live console fixtures ---------------------------------------------------------------------
def test_live_relays():
    on = Device(0xE1, 30, 4, "00" * 6, 0x40, H("81ff00010000"))     # electric water heater, ON
    off = Device(0x54, 30, 61, "00" * 6, 0x10, H("80ff00010000"))    # ceiling light, OFF
    assert on.is_on and not off.is_on
    assert on.is_switch and on.current == pytest.approx(1 / 256)
    assert not on.is_faulted and on.dtc_code == 0


def test_live_faulted_awning():
    d = Device(0xA9, 33, 105, "00" * 6, 0x11, H("e0ff00000653"))
    assert d.is_cover and d.is_faulted and d.dtc_code == 0x0653
    assert d.position is None           # 0xff = no position feedback
    assert d.current == 0.0


def test_live_tanks():
    assert Device(0xBC, 10, 69, "00" * 6, 0x20, H("64")).fill_level == 100
    assert Device(0xDF, 10, 68, "00" * 6, 0x31, H("21")).fill_level == 33
    # the high bit is not part of the level (the app masks it off)
    assert Device(0xDF, 10, 68, "00" * 6, 0x31, H("a1")).fill_level == 33


# ---- relays ---------------------------------------------------------------------------------------
def test_relay_type1():
    assert P.relay_cmd(3, True) == (0, b"\x83")
    assert P.relay_cmd(3, False) == (0, b"\x80")
    assert P.relay_is_on(3, b"\x01") and not P.relay_is_on(3, b"\x00")
    assert P.relay_faulted(3, b"\x40") and not P.relay_faulted(3, b"\x20")
    assert P.relay_current(3, b"\x01") is None
    assert P.clear_fault_cmd(3) == (0, b"\x40")


def test_relay_type2():
    assert P.relay_cmd(30, True) == (1, b"")
    assert P.relay_cmd(30, False) == (0, b"")
    assert P.clear_fault_cmd(33) == (3, b"")
    assert P.relay_current(30, H("81ffffff0000")) is None   # 0xffff = not supported


# ---- h-bridges ------------------------------------------------------------------------------------
def test_hbridge_type2_commands():
    assert P.move_cmd(33, P.MOVE_FORWARD) == (1, b"")
    assert P.move_cmd(33, P.MOVE_REVERSE) == (2, b"")
    assert P.move_cmd(33, P.MOVE_STOP) == (0, b"")


def test_hbridge_type1_relay_mapping():
    # default classes (slide, awning): forward = relay 1 (0x01), reverse = relay 2 (0x04)
    assert P.move_cmd(6, P.MOVE_FORWARD, 10) == (0, b"\x01")
    assert P.move_cmd(6, P.MOVE_REVERSE, 10) == (0, b"\x04")
    assert P.move_cmd(6, P.MOVE_STOP, 10) == (0, b"\x00")
    # stabilizer / generator / landing gear / vent cover are wired the other way round
    for fc in (P.FC_STABILIZER, P.FC_GENERATOR, P.FC_LANDING_GEAR, P.FC_VENT_COVER):
        assert P.move_cmd(6, P.MOVE_FORWARD, fc) == (0, b"\x04")
        assert P.move_cmd(6, P.MOVE_REVERSE, fc) == (0, b"\x01")
    # doors: swapped only for ramps
    assert P.move_cmd(6, P.MOVE_FORWARD, P.FC_DOOR, "Entry Door") == (0, b"\x01")
    assert P.move_cmd(6, P.MOVE_FORWARD, P.FC_DOOR, "Ramp Door") == (0, b"\x04")


# ---- lights ---------------------------------------------------------------------------------------
def test_dimmer():
    s = H("01c800c800000000")
    d = P.dimmer(s)
    assert d["on"] and d["max_brightness"] == 200 and d["brightness"] == 200
    assert P.dimmer_cmd(P.DIM_ON, 128, 0) == (0, H("0180000000000000"))
    assert P.dimmer_cmd(P.DIM_RESTORE) == (0, H("7f00000000000000"))
    # blink/swell get the app's default 220 ms cycle when none is given
    assert P.dimmer_cmd(P.DIM_BLINK, 255) == (0, H("02ff0000dc00dc00"))


def test_rgb():
    d = P.rgb(H("01ff800000000000"))
    assert d["on"] and d["rgb"] == (255, 128, 0)
    assert P.rgb_cmd(P.RGB_ON, (1, 2, 3), 0, 0) == (0, H("0101020300000000"))
    assert P.rgb_cmd(P.RGB_EFFECTS["Rainbow"], interval=750) == (0, H("08000000" "0002ee00"))


def test_rgbw_roundtrip():
    cmd = P.rgbw_cmd(P.RGB_ON, hue=240, saturation=100, value=50)
    assert cmd[0] == 0 and len(cmd[1]) == 8
    d = P.rgbw(cmd[1])
    assert (d["hue"], d["saturation"], d["value"], d["on"]) == (240, 100, 50, True)
    assert P.rgbw(P.rgbw_cmd(P.RGB_ON, 359, 0, 100)[1])["hue"] == 359


# ---- HVAC -------------------------------------------------------------------------------------------
def test_hvac():
    cmd = P.hvac_cmd(P.HVAC_BOTH, P.SRC_HEAT_PUMP, P.FAN_LOW, 68, 76)
    assert cmd == (0, bytes([0x03 | 0x10 | 0x80, 68, 76]))
    status = bytes([0x93, 68, 76, 0x02]) + (72 * 256).to_bytes(2, "big") + \
        ((-5 * 256) & 0xFFFF).to_bytes(2, "big")
    d = P.hvac(status)
    assert d["heat_mode"] == P.HVAC_BOTH and d["heat_source"] == P.SRC_HEAT_PUMP
    assert d["fan_mode"] == P.FAN_LOW and d["zone_status"] == "cooling"
    assert d["indoor_f"] == 72.0 and d["outdoor_f"] == -5.0
    assert P.hvac(status[:4] + H("8000") + status[6:])["indoor_f"] is None   # invalid sensor


# ---- generator ------------------------------------------------------------------------------------
def test_generator():
    d = P.generator(bytes([0x83]) + (int(12.5 * 256)).to_bytes(2, "big") + H("8000"))
    assert d["state"] == "running" and d["quiet_hours"]
    assert d["battery_v"] == 12.5 and d["temperature_f"] is None
    assert P.generator_cmd(True) == (0, b"\x02") and P.generator_cmd(False) == (0, b"\x01")


# ---- sensors --------------------------------------------------------------------------------------
def test_temperature_sensor():
    d = P.temperature(((int(-2.5 * 256)) & 0xFFFF).to_bytes(2, "big") + bytes([192, 80, 0x80, 0, 0, 0]))
    assert d["temperature_c"] == -2.5 and d["battery_v"] == 3.0 and d["battery_pct"] == 80
    assert d["low_battery"]


def test_hour_meter():
    d = P.hour_meter((3600 * 250).to_bytes(4, "big") + b"\x03")
    assert d["hours"] == 250 and d["running"] and d["maintenance_due"] and d["valid"]
    assert not P.hour_meter(b"\x00\x00\x00\x00\x80")["valid"]


def test_battery_monitor():
    cur = (-10 * 256) & 0xFFFFFF
    d = P.battery((int(12.8 * 256)).to_bytes(2, "big") + cur.to_bytes(3, "big") + bytes([87, 0, 0]))
    assert d["voltage"] == pytest.approx(12.8, abs=0.01) and d["current"] == -10 and d["soc"] == 87
    assert P.battery(H("0000000000ff0000"))["soc"] is None          # still computing
    assert P.battery(H("0000000000d00000"))["soc"] == 0x50          # estimate (bit 7 set)


def test_fan():
    d = P.fan(bytes([1, (-40) & 0xFF, 50]))
    assert d["speed"] == 40 and d["direction"] == "reverse" and d["vent"] == 50
    assert P.fan_speed_cmd(40, reverse=True) == (0, bytes([(-40) & 0xFF]))
    assert P.fan_vent_cmd(60) == (1, bytes([50]))


def test_chassis_and_lock():
    c = P.chassis(bytes([0x06, 0, 0xC8, 0xFF]))
    assert c["park_brake"] == "engaged" and c["ignition"] == "on"
    assert c["towable_battery_v"] == 12.5 and c["towable_brake_v"] is None
    assert P.chassis(b"\x00") == {"park_brake": None, "ignition": None,
                                  "towable_battery_v": None, "towable_brake_v": None}
    lk = P.lock(bytes([0x02, 90, 0x80, 0]))
    assert lk["state"] == "locked" and lk["low_battery"]
