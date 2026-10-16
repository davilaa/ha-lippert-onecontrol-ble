"""Load the integration in a real Home Assistant with a fake bus and drive every platform.

Needs pytest-homeassistant-custom-component (skipped when it isn't installed).
"""
from unittest.mock import AsyncMock, PropertyMock, patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from custom_components.compass_console import protocol as P  # noqa: E402
from custom_components.compass_console.compass_ble import Device  # noqa: E402
from custom_components.compass_console.const import DOMAIN  # noqa: E402
from custom_components.compass_console.tables import FNCLASS  # noqa: E402

ADDR = "AA:BB:CC:DD:EE:FF"
MAC = "0011223344aa"
H = bytes.fromhex


def _f(fclass):
    return FNCLASS.index(fclass)


def _devices():
    devs = [
        Device(0x54, 30, 61, MAC, 0x10, H("81ff00010000")),                 # ceiling light, on
        Device(0x55, 3, 7, MAC, 0x21, H("00")),                             # type-1 relay "Light 1"
        Device(0x86, 33, 105, MAC, 0x22, H("c0ff00000000")),                # awning 2
        Device(0x87, 6, 96, MAC, 0x31, H("00")),                            # type-1 slide 1
        Device(0x88, 33, _f(P.FC_GENERATOR), MAC, 0x40, H("c0ff00000000")), # relay generator
        Device(0x89, 12, _f(P.FC_GENERATOR), MAC, 0x40, (3600 * 12).to_bytes(4, "big") + b"\x00"),
        Device(0x3D, 10, 67, MAC, 0x10, H("21")),                           # fresh tank 33 %
        Device(0x60, 20, 7, MAC, 0x02, H("01c800c800000000")),              # dimmer, on 200
        Device(0x61, 13, 7, MAC, 0x03, H("00ff000000000000")),              # rgb, off
        Device(0x62, 61, 7, MAC, 0x04, P.rgbw_cmd(1, 120, 100, 50)[1]),     # rgbw, on
        Device(0x63, 16, _f(20), MAC, 0x10,
               bytes([0x01, 66, 78, 0x05]) + (70 * 256).to_bytes(2, "big") + H("8000"),
               caps=P.HVAC_CAP_GAS | P.HVAC_CAP_AC),
        Device(0x64, 24, _f(P.FC_GENERATOR), MAC, 0x10,
               bytes([0x00]) + (int(12.5 * 256)).to_bytes(2, "big") + H("8000")),
        Device(0x65, 25, _f(25), MAC, 0x10, (20 * 256).to_bytes(2, "big") + bytes([192, 90, 0, 0, 0, 0])),
        Device(0x66, 37, _f(28), MAC, 0x10, bytes([1, 50, 100]), caps=0x11),
        Device(0x67, 49, _f(26), MAC, 0x10,
               (int(13.2 * 256)).to_bytes(2, "big") + (5 * 256).to_bytes(3, "big") + bytes([95, 0, 0])),
        Device(0x68, 39, 0, MAC, 0x0E, bytes([0x06, 0, 0xC8, 0xFF])),
        Device(0x69, 51, _f(3), MAC, 0x10, bytes([0x02, 80, 0, 0])),
        Device(0x6A, 27, _f(26), MAC, 0x11, (int(12.6 * 65536)).to_bytes(4, "big")[:2] + H("0005") + bytes([80, 0, 60])),
    ]
    return {d.addr: d for d in devs}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
async def setup(hass: HomeAssistant):
    hass.config.components.add("bluetooth_adapters")   # don't need a real BT stack here
    from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
    hass.config.units = US_CUSTOMARY_SYSTEM
    devmap = _devices()
    with patch("custom_components.compass_console.compass_ble.CompassClient.async_enumerate",
               AsyncMock(return_value=devmap)), \
         patch("custom_components.compass_console.compass_ble.CompassClient.async_command",
               AsyncMock(return_value=True)) as cmd, \
         patch("custom_components.compass_console.compass_ble.CompassClient.async_set",
               AsyncMock(return_value=(True, None))) as rset, \
         patch("custom_components.compass_console.compass_ble.CompassClient.async_move",
               AsyncMock(return_value=(1.0, False))) as move, \
         patch("custom_components.compass_console.compass_ble.CompassClient.async_close",
               AsyncMock()), \
         patch("custom_components.compass_console.compass_ble.CompassClient.devices",
               new_callable=PropertyMock, return_value=list(devmap.values())):
        entry = MockConfigEntry(domain=DOMAIN, data={"address": ADDR, "scan_interval": 60},
                                unique_id=ADDR)
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        yield {"cmd": cmd, "set": rset, "move": move, "devmap": devmap}


def _ids(hass, domain):
    return sorted(s.entity_id for s in hass.states.async_all(domain))


async def test_entities_created(hass, setup):
    assert hass.states.get("switch.ceiling_light").state == "on"
    assert hass.states.get("switch.light_1").state == "off"
    assert "cover.awning_2" in _ids(hass, "cover")
    assert "cover.slide_1" in _ids(hass, "cover")
    assert not any("generator" in e for e in _ids(hass, "cover"))   # relay generator isn't a cover
    assert {"button.generator_start", "button.generator_stop"} <= set(_ids(hass, "button"))
    assert hass.states.get("light.light_2").attributes["brightness"] == 200
    assert hass.states.get("light.light_3").state == "off"
    rgbw = hass.states.get("light.light_4")
    assert rgbw.state == "on" and tuple(rgbw.attributes["hs_color"]) == (120.0, 100.0)
    clim = hass.states.get("climate.thermostat") or next(
        hass.states.get(e) for e in _ids(hass, "climate"))
    assert clim.state == "heat" and clim.attributes["temperature"] == 66
    assert clim.attributes["current_temperature"] == 70
    assert "heat_cool" in clim.attributes["hvac_modes"]
    fan = next(hass.states.get(e) for e in _ids(hass, "fan"))
    assert fan.state == "on" and fan.attributes["percentage"] == 50
    assert any(e.endswith("_lid") for e in _ids(hass, "cover"))
    sensors = {e: hass.states.get(e).state for e in _ids(hass, "sensor")}
    assert sensors["sensor.fresh_tank"] == "33"
    assert any(v == "12.0" for k, v in sensors.items() if k.endswith("_hours"))
    assert sensors.get("sensor.chassis_park_brake") == "engaged"
    assert any(v == "95" for k, v in sensors.items() if k.endswith("_charge"))
    assert any(v == "locked" for v in sensors.values())


async def test_switch_commands_use_key(hass, setup):
    await hass.services.async_call("switch", "turn_off", {"entity_id": "switch.light_1"},
                                   blocking=True)
    await hass.async_block_till_done()
    setup["set"].assert_awaited_with("key:7_2_1", False)


async def test_dimmer_brightness(hass, setup):
    await hass.services.async_call("light", "turn_on",
                                   {"entity_id": "light.light_2", "brightness": 128}, blocking=True)
    key, command, _confirm = setup["cmd"].await_args.args
    assert key == "7_0_2_t20" and command == (0, H("0180000000000000"))


async def test_dimmer_restore_and_effect(hass, setup):
    await hass.services.async_call("light", "turn_on", {"entity_id": "light.light_2"}, blocking=True)
    assert setup["cmd"].await_args.args[1] == (0, H("7f00000000000000"))
    await hass.services.async_call("light", "turn_on",
                                   {"entity_id": "light.light_2", "effect": "Swell"}, blocking=True)
    assert setup["cmd"].await_args.args[1][1][0] == P.DIM_SWELL


async def test_rgb_color(hass, setup):
    await hass.services.async_call("light", "turn_on",
                                   {"entity_id": "light.light_3", "rgb_color": [255, 0, 0],
                                    "brightness": 255}, blocking=True)
    assert setup["cmd"].await_args.args[1] == (0, H("01ff000000000000"))


async def test_climate(hass, setup):
    eid = _ids(hass, "climate")[0]
    await hass.services.async_call("climate", "set_temperature",
                                   {"entity_id": eid, "temperature": 70}, blocking=True)
    assert setup["cmd"].await_args.args[1] == (0, bytes([0x01, 70, 78]))
    await hass.services.async_call("climate", "set_hvac_mode",
                                   {"entity_id": eid, "hvac_mode": "cool"}, blocking=True)
    assert setup["cmd"].await_args.args[1] == (0, bytes([0x02, 66, 78]))


async def test_fan(hass, setup):
    eid = _ids(hass, "fan")[0]
    await hass.services.async_call("fan", "set_percentage",
                                   {"entity_id": eid, "percentage": 30}, blocking=True)
    assert setup["cmd"].await_args.args[1] == (0, bytes([30]))


async def test_generator_genie(hass, setup):
    eid = next(e for e in _ids(hass, "switch") if "generator" in e)
    await hass.services.async_call("switch", "turn_on", {"entity_id": eid}, blocking=True)
    assert setup["cmd"].await_args.args[1] == (0, b"\x02")


async def test_relay_generator_start(hass, setup):
    await hass.services.async_call("button", "press", {"entity_id": "button.generator_start"},
                                   blocking=True)
    args, kwargs = setup["move"].await_args
    assert args[1] == P.MOVE_FORWARD and callable(kwargs["until"])
    assert args[2] == 10.0          # hour meter on the same module -> hold up to 10 s
