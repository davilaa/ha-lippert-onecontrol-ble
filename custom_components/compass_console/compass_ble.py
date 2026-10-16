"""Reusable async BLE client for the Lippert OneControl / IDS-CAN 'Compass Console'.
Deployment-agnostic: works standalone (BleakClient by address) or inside Home Assistant
(pass a ble_device getter from homeassistant.components.bluetooth). Verified flow:
connect -> KeySeed unlock -> address-claim + presence heartbeat -> RemoteControl session -> command.
"""
import asyncio
import logging
import time
from bleak import BleakClient

try:
    from . import protocol as P
    from .tables import DT, FN, FNCLASS
except ImportError:  # pragma: no cover - standalone import
    import protocol as P
    from tables import DT, FN, FNCLASS

_LOG = logging.getLogger(__name__)


def _u(x):
    return f"{x:08x}-0200-a58e-e411-afe28044e62c"


CH_IN = _u(0x0001)
CH_OUT = _u(0x0002)
CH_SEED = _u(0x0012)
CH_KEY = _u(0x0013)
_M = 0xFFFFFFFF
KEYSEED_CYPHER = 3357376288       # X180T gateway ECU unlock
REMOTE_CTRL_CYPHER = 2976579765   # SESSION_ID.REMOTE_CONTROL = 4
PROTO_VER = 0x1C                  # local-host IDS-CAN protocol version (28)
WARM_SECONDS = 12                 # keep the BLE link warm this long after a command
SESSION_WARM_SECONDS = 2          # hold a RemoteControl session open this long after a command,
                                  # then END it. While we hold a session the device ignores its
                                  # physical console buttons, so keep this short (it only saves the
                                  # handshake on quick back-to-back taps / cover reversals)
ALIASES = {
    "ceiling": 61, "exterior": 143, "waterheater": 4, "ewh": 4,
    "electricwaterheater": 4, "gaswaterheater": 3, "gwh": 3,
    "waterpump": 5, "pump": 5,
}


def _encrypt(cypher, seed):
    num = 0x9E3779B9
    for _ in range(32):
        t1 = ((cypher << 4) + 0x436F7079) & _M
        t2 = (cypher + num) & _M
        t3 = ((cypher >> 5) + 0x72696768) & _M
        seed = (seed + (t1 ^ t2 ^ t3)) & _M
        s1 = ((seed << 4) + 0x74204944) & _M
        s2 = (seed + num) & _M
        s3 = ((seed >> 5) + 0x53736E63) & _M
        cypher = (cypher + (s1 ^ s2 ^ s3)) & _M
        num = (num + 0x9E3779B9) & _M
    return seed


def _is_unlocked(b):
    return b is not None and len(b) == 8 and b.lower() == b"unlocked"


def _a29(mt, src, tgt, ext, pl=b""):
    idv = ((((mt & 0x1C) << 24) | (src << 18) | ((mt & 3) << 16) | (tgt << 8) | ext) & 0x7FFFFFFF) | 0x80000000
    return bytes([len(pl), (idv >> 24) & 0xFF, (idv >> 16) & 0xFF, (idv >> 8) & 0xFF, idv & 0xFF]) + pl


def _a11(mtype, addr, pl=b""):
    idv = ((mtype & 0xFF) << 8) | (addr & 0xFF)
    return bytes([len(pl), (idv >> 8) & 0xFF, idv & 0xFF]) + pl


def _src_of(b):
    if not b:
        return None
    if b[0] == 1 and len(b) >= 2:
        return b[1]
    if b[0] == 2 and len(b) >= 5:
        return b[4]
    if b[0] == 3 and len(b) >= 5:
        idv = (b[1] << 24) | (b[2] << 16) | (b[3] << 8) | b[4]
        return (idv >> 18) & 0xFF
    return None


def _build_justworks_agent():
    """A BlueZ Agent1 that auto-accepts Just-Works pairing (NoInputNoOutput)."""
    from dbus_fast.service import ServiceInterface, method

    class _Agent(ServiceInterface):
        def __init__(self):
            super().__init__("org.bluez.Agent1")

        @method()
        def Release(self):
            return

        @method()
        def RequestPinCode(self, device: "o") -> "s":  # noqa: F821,N802
            return "0000"

        @method()
        def DisplayPinCode(self, device: "o", pincode: "s"):  # noqa: F821,N802
            return

        @method()
        def RequestPasskey(self, device: "o") -> "u":  # noqa: F821,N802
            return 0

        @method()
        def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):  # noqa: F821,N802
            return

        @method()
        def RequestConfirmation(self, device: "o", passkey: "u"):  # noqa: F821,N802
            return

        @method()
        def RequestAuthorization(self, device: "o"):  # noqa: F821,N802
            return

        @method()
        def AuthorizeService(self, device: "o", uuid: "s"):  # noqa: F821,N802
            return

        @method()
        def Cancel(self):
            return

    return _Agent()


MOVE_STOP, MOVE_FORWARD, MOVE_REVERSE = P.MOVE_STOP, P.MOVE_FORWARD, P.MOVE_REVERSE


class Device:
    def __init__(self, addr, dtype, func, mac, inst, status, pid=0, caps=0):
        self.addr = addr
        self.dtype = dtype
        self.func = func
        self.mac = mac
        self.product_id = pid            # PRODUCT_ID wire code (module/controller model)
        self.caps = caps                 # raw device-capability byte (meaning depends on type)
        self.dinst = (inst >> 4) & 0xF   # device instance
        self.finst = inst & 0xF          # function instance
        self.status = bytes(status)
        self.state = self.status[0] if self.status else 0

    @property
    def key(self):
        """Stable identity across console power-cycles (CAN address changes, this doesn't).
        Devices on one module can share function + instances (a generator relay and its hour
        meter), so newer kinds add the device type; relays / covers / tanks keep the original
        format so existing entity IDs and saved cover settings stay valid."""
        base = f"{self.func}_{self.dinst}_{self.finst}"
        if self.kind in (P.K_RELAY, P.K_COVER, P.K_TANK, None):
            return base
        return f"{base}_t{self.dtype}"

    @property
    def name(self):
        base = FN.get(self.func, f"func{self.func}")
        return f"{base} {self.finst}" if self.finst else base

    @property
    def type_name(self):
        return DT.get(self.dtype, f"type{self.dtype}")

    @property
    def fclass(self):
        return FNCLASS[self.func] if 0 <= self.func < len(FNCLASS) else 0

    @property
    def kind(self):
        """What this device is to us (protocol.K_*), or None if not exposed."""
        return P.device_kind(self.dtype, self.fclass)

    @property
    def is_switch(self):
        return self.kind == P.K_RELAY

    @property
    def is_tank(self):
        return self.kind == P.K_TANK

    @property
    def is_cover(self):
        return self.kind == P.K_COVER

    @property
    def _is_output(self):
        return self.kind in (P.K_RELAY, P.K_COVER, P.K_RELAY_GENERATOR)

    @property
    def position(self):
        """Output position percent if the device reports it, else None."""
        return P.hbridge_position(self.dtype, self.status) if self._is_output else None

    @property
    def current(self):
        """Output current draw in amps for relays / h-bridges that report it, else None."""
        return P.relay_current(self.dtype, self.status) if self._is_output else None

    @property
    def is_on(self):
        return P.relay_is_on(self.dtype, self.status)

    @property
    def is_moving(self):
        return P.hbridge_moving(self.dtype, self.status)

    @property
    def is_faulted(self):
        """The output is latched off by a fault and ignores commands until cleared."""
        return P.relay_faulted(self.dtype, self.status) if self._is_output else False

    @property
    def dtc_code(self):
        """Raw diagnostic trouble code reported by the device; 0 = none."""
        if self._is_output:
            return P.relay_dtc(self.dtype, self.status)
        if self.kind == P.K_GENERATOR:
            g = P.generator(self.status)
            return g["dtc"] if g else 0
        return 0

    @property
    def fill_level(self):
        """Tank fill percentage (0-100), or None if invalid/not a tank."""
        return P.tank_level(self.status) if self.is_tank else None

    @property
    def data(self):
        """Decoded status for the richer device kinds (dict), or None."""
        decoder = _DECODERS.get(self.kind)
        return decoder(self.status) if decoder else None

    def __repr__(self):
        return f"<{self.name} 0x{self.addr:02x} type{self.dtype} state=0x{self.state:02x}>"


_DECODERS = {
    P.K_TANK: lambda s: {"level": P.tank_level(s), "battery": P.tank_battery(s),
                         "quality": P.tank_quality(s)},
    P.K_HOUR_METER: P.hour_meter, P.K_DIMMER: P.dimmer, P.K_RGB: P.rgb, P.K_RGBW: P.rgbw,
    P.K_HVAC: P.hvac, P.K_GENERATOR: P.generator, P.K_TEMPERATURE: P.temperature,
    P.K_AC_POWER: P.ac_power, P.K_DC_POWER: P.dc_power, P.K_FAN: P.fan,
    P.K_CHASSIS: P.chassis, P.K_BATTERY: P.battery, P.K_LOCK: P.lock,
}


class CompassClient:
    """One BLE session at a time (the console allows a single connection), guarded by a lock.

    ble_device_getter: optional async callable(address) -> BLEDevice for Home Assistant;
    if omitted, connects directly by address (standalone).
    """

    def __init__(self, address, mac=b"\x02\x00\x5e\xa0\x00\x01", ble_device_getter=None):
        self.address = address
        self.mac = mac
        self._get_dev = ble_device_getter
        self._lock = asyncio.Lock()
        self._wlock = asyncio.Lock()
        self._frames = []
        self._devmap = {}
        self._inuse = set()
        self._hb = False
        self._dumped = False
        self._stop_events = {}
        # warm-connection state (persistent: the link is kept always-on)
        self._conn = None
        self._conn_aa = None
        self._conn_hb = None
        self._idle_task = None
        self._keep_warm = True
        self._released = False   # True = deliberately let go of the console for the phone app
        self._sup_task = None
        # Warm RemoteControl sessions: {device_addr: keepalive_expiry_monotonic}. While an entry
        # is live the heartbeat loop "tends" it (ext-68) so the next command to that device
        # reuses the open session and skips the seed/key handshake. Sessions are per-device with
        # NO global limit and no max lifetime -- so we hold as many as we like, each "hot" as
        # long as it's tended, and any command to a hot device is instant.
        self._sessions = {}

    def _cb(self, _ch, data):
        b = bytes(data)
        self._frames.append(b)
        if len(b) >= 20 and b[0] == 1:
            slen = b[18]
            status = b[19:19 + slen] if slen else b[19:20]
            self._devmap[b[1]] = Device(b[1], b[13], (b[14] << 8) | b[15],
                                        b[4:10].hex(), b[16], status,
                                        (b[10] << 8) | b[11],  # product id (announce rx[0:1])
                                        b[17])                 # device capability byte
        s = _src_of(b)
        if s:
            self._inuse.add(s)

    async def _make_client(self):
        if self._get_dev is not None:
            dev = await self._get_dev(self.address)
            if dev is None:
                raise RuntimeError("BLE device not found by HA bluetooth")
            # Use HA's connection-slot coordinator; let errors propagate to _connect's retry.
            from bleak_retry_connector import establish_connection
            return await establish_connection(BleakClient, dev, self.address)
        c = BleakClient(self.address, timeout=20)
        await c.connect()
        return c

    async def _connect(self):
        last = None
        for _ in range(6):
            try:
                return await self._make_client()
            except Exception as e:
                last = e
                _LOG.debug("connect retry: %s", e)
                await asyncio.sleep(1.5)
        raise last or RuntimeError("connect failed")

    async def _wr(self, c, d):
        # Serialize writes: the heartbeat task and the session handshake both write to
        # char 0001; concurrent writes corrupt the exchange on HA's bluetooth stack.
        async with self._wlock:
            await asyncio.wait_for(c.write_gatt_char(CH_IN, d, response=True), 8)

    async def _wr_nr(self, c, d):
        # Fast write-without-response, for the rapid movement-pulse stream.
        async with self._wlock:
            await asyncio.wait_for(c.write_gatt_char(CH_IN, d, response=False), 4)

    async def _rd(self, c, x):
        try:
            return bytes(await asyncio.wait_for(c.read_gatt_char(x), 8))
        except Exception:
            return b""

    async def _unlock(self, c):
        for _ in range(4):
            sb = await self._rd(c, CH_SEED)
            if _is_unlocked(sb):
                return True
            if len(sb) < 4:
                await asyncio.sleep(0.4)
                continue
            s = int.from_bytes(sb[:4], "big")
            if s == 0:
                break
            try:
                await asyncio.wait_for(
                    c.write_gatt_char(CH_KEY, _encrypt(KEYSEED_CYPHER, s).to_bytes(4, "big"), response=True), 8)
            except Exception:
                pass
            await asyncio.sleep(0.3)
        return _is_unlocked(await self._rd(c, CH_SEED))

    async def _wait(self, aa, ext, timeout=2.5):
        n0 = len(self._frames)
        for _ in range(int(timeout * 40)):
            for b in self._frames[n0:]:
                if len(b) >= 6 and b[0] == 3 and b[3] == aa and b[4] == ext:
                    return b
            await asyncio.sleep(0.025)
        return None

    def _pick_addr(self):
        for cand in (0xA0, 0xA5, 0xAA, 0xB0, 0xC0, 0xE0, 0xE5, 0x90, 0x70, 0xB5, 0xD0):
            if cand not in self._inuse:
                return cand
        return 0xA0

    def _session_warm(self, addr):
        """True if a RemoteControl session to addr is currently open (transient or permanent)."""
        return self._sessions.get(addr, 0.0) > time.monotonic()

    def _mark_session(self, addr):
        """(Re)start the keepalive window for addr's session. Sessions are per-device with no
        global limit, so we keep EVERY hot device in the dict at once -- the heartbeat tends
        them all, so any recently-used device answers commands instantly (no handshake)."""
        self._sessions[addr] = time.monotonic() + SESSION_WARM_SECONDS

    async def _end_session(self, c, aa, addr):
        """Best-effort SESSION_END so the device's own console works again immediately."""
        try:
            await self._wr_nr(c, _a29(128, aa, addr, 69, b"\x00\x04"))
        except Exception:  # noqa: BLE001
            pass

    async def _heartbeat(self, c, aa):
        net = _a11(0, aa, bytes([0x00, PROTO_VER]) + self.mac)
        n = 0
        while self._hb:
            try:
                if not c.is_connected:
                    break
                await self._wr_nr(c, net)  # presence every 0.8s (write-without-response, like the app)
                n += 1
                # Expired sessions get an explicit SESSION_END (ext 69) right away so the device
                # hands control back to its physical console now, not after the 5s timeout.
                now = time.monotonic()
                for a in list(self._sessions):
                    if self._sessions.get(a, 0.0) <= now:
                        self._sessions.pop(a, None)
                        await self._end_session(c, aa, a)
                # Tend the still-hot sessions ~every 2.4s (device timeout is 5s) so a quick
                # follow-up command skips the handshake. Write-without-response = cheap.
                if n % 3 == 0:
                    for a in list(self._sessions):
                        try:
                            await self._wr_nr(c, _a29(128, aa, a, 68, b"\x00\x04"))
                        except Exception:  # noqa: BLE001
                            self._sessions.pop(a, None)
            except Exception:
                pass
            await asyncio.sleep(0.8)

    # ---- warm connection management -------------------------------------------------
    async def _ensure_warm(self):
        """Return a live (client, claimed_addr), reusing the warm link or establishing one."""
        if self._released:
            # Console handed to the phone app -> refuse to grab the link (a stray command from
            # an automation must not reconnect behind the app's back). Reclaim clears this first.
            raise RuntimeError("console released for the phone app; reclaim it in HA first")
        if self._conn is not None:
            try:
                if self._conn.is_connected:
                    return self._conn, self._conn_aa
            except Exception:
                pass
            await self._teardown_warm()
        c = await self._open_and_learn(1.2)              # connect + unlock + subscribe + listen
        aa = self._pick_addr()
        await self._wr(c, _a11(0, 0, bytes([aa, PROTO_VER]) + self.mac))  # address claim
        self._conn = c
        self._conn_aa = aa
        self._hb = True
        self._conn_hb = asyncio.create_task(self._heartbeat(c, aa))
        await asyncio.sleep(1.4)                          # let presence establish (fresh link only)
        self._ensure_supervisor()
        return c, aa

    def _ensure_supervisor(self):
        """Start the keepalive task that keeps the link always-on (reconnects on drop)."""
        if self._keep_warm and (self._sup_task is None or self._sup_task.done()):
            self._sup_task = asyncio.create_task(self._keepalive())

    async def _keepalive(self):
        """Persistent link: re-warm proactively if it ever drops, so the next command
        never pays the cold connect+unlock cost."""
        while self._keep_warm:
            await asyncio.sleep(10)
            try:
                alive = self._conn is not None and self._conn.is_connected
            except Exception:  # noqa: BLE001
                alive = False
            if alive:
                continue
            async with self._lock:
                try:
                    alive = self._conn is not None and self._conn.is_connected
                except Exception:  # noqa: BLE001
                    alive = False
                if not alive:
                    try:
                        await self._ensure_warm()
                    except Exception as e:  # noqa: BLE001
                        _LOG.debug("compass keepalive reconnect failed: %s", e)

    async def _teardown_warm(self):
        self._hb = False
        if self._conn_hb is not None:
            try:
                await self._conn_hb
            except Exception:
                pass
            self._conn_hb = None
        if self._conn is not None:
            try:
                if self._conn.is_connected and self._conn_aa is not None:
                    for a in list(self._sessions):
                        await self._end_session(self._conn, self._conn_aa, a)
            except Exception:  # noqa: BLE001
                pass
            try:
                await self._conn.disconnect()
            except Exception:
                pass
        self._conn = None
        self._conn_aa = None
        self._sessions.clear()  # link gone -> all sessions invalid

    def _touch_idle(self):
        # Persistent (always-on) link: never idle-disconnect; just ensure keepalive runs.
        self._ensure_supervisor()

    @property
    def is_connected(self):
        """True if we currently hold a live warm BLE link to the console."""
        try:
            return self._conn is not None and self._conn.is_connected
        except Exception:  # noqa: BLE001
            return False

    @property
    def devices(self):
        """Live snapshot of every device currently heard on the bus."""
        return list(self._devmap.values())

    @property
    def is_released(self):
        """True while we've deliberately let go of the console (so the phone app can use it)."""
        return self._released

    def set_released_initial(self, released):
        """Apply a persisted 'released' state at setup time (before any connection), so a release
        survives an HA restart. Only sets flags -- the first poll (async_enumerate) then simply
        won't connect while released; it does NOT touch the link here."""
        self._released = bool(released)
        if released:
            self._keep_warm = False

    async def async_release(self):
        """Let go of the console so the Compass Connect phone app can connect. The console
        allows only ONE Bluetooth connection, so we disconnect and STOP reconnecting (the
        keepalive supervisor and enumeration back off) until async_reclaim()."""
        self._released = True
        self._keep_warm = False
        if self._sup_task is not None:
            self._sup_task.cancel()
            self._sup_task = None
        async with self._lock:
            await self._teardown_warm()

    async def async_reclaim(self):
        """Take the console back after app use: reconnect and resume the always-on link. Starts
        the keepalive supervisor first so it keeps retrying even if this first attempt raises
        (e.g. the app re-paired and evicted our bond -> the link comes back once the user
        re-pairs via the Pair button with the console in pairing mode)."""
        self._released = False
        self._keep_warm = True
        self._ensure_supervisor()  # retry loop runs regardless of the immediate attempt below
        async with self._lock:
            await self._ensure_warm()

    async def async_close(self):
        """Tear down the warm link, keepalive, and timers (call on HA unload)."""
        self._keep_warm = False
        if self._sup_task is not None:
            self._sup_task.cancel()
            self._sup_task = None
        if self._idle_task is not None:
            self._idle_task.cancel()
            self._idle_task = None
        async with self._lock:
            await self._teardown_warm()

    async def _find_device(self, bus, wait=0.0):
        """Poll BlueZ for our console. Returns (dev_path, adapter_path, paired_bool)."""
        tgt = self.address.upper()
        remaining = wait
        while True:
            intro = await bus.introspect("org.bluez", "/")
            om = bus.get_proxy_object("org.bluez", "/", intro).get_interface(
                "org.freedesktop.DBus.ObjectManager")
            objects = await om.call_get_managed_objects()
            for path, ifaces in objects.items():
                d = ifaces.get("org.bluez.Device1")
                if d and "Address" in d and d["Address"].value.upper() == tgt:
                    paired = bool(d["Paired"].value) if "Paired" in d else False
                    return path, path.rsplit("/", 1)[0], paired
            if remaining <= 0:
                return None, None, False
            await asyncio.sleep(0.5)
            remaining -= 0.5

    async def async_pair(self):
        """Register a Just-Works pairing agent, clear any stale bond, and (re)pair via
        BlueZ. Console must be in pairing mode. Returns True on success."""
        from dbus_fast.aio import MessageBus
        from dbus_fast.constants import BusType
        from dbus_fast import Variant

        async with self._lock:
            await self._teardown_warm()
            bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            agent_path = "/compass_console/agent"
            am = None
            registered = False
            try:
                bus.export(agent_path, _build_justworks_agent())
                am_intro = await bus.introspect("org.bluez", "/org/bluez")
                am = bus.get_proxy_object("org.bluez", "/org/bluez", am_intro).get_interface(
                    "org.bluez.AgentManager1")
                await am.call_register_agent(agent_path, "NoInputNoOutput")
                await am.call_request_default_agent(agent_path)
                registered = True
                _LOG.warning("compass: pairing agent registered")

                dev_path, adapter_path, paired = await self._find_device(bus, wait=8.0)
                if dev_path is not None and paired:
                    try:
                        ai = await bus.introspect("org.bluez", adapter_path)
                        await bus.get_proxy_object("org.bluez", adapter_path, ai).get_interface(
                            "org.bluez.Adapter1").call_remove_device(dev_path)
                        _LOG.warning("compass: removed stale bond %s", dev_path)
                    except Exception as e:  # noqa: BLE001
                        _LOG.warning("compass: remove failed: %s", e)
                    dev_path = None
                if dev_path is None:
                    dev_path, adapter_path, paired = await self._find_device(bus, wait=12.0)
                if dev_path is None:
                    _LOG.warning("compass: console not discoverable for pairing")
                    return False

                dintro = await bus.introspect("org.bluez", dev_path)
                dobj = bus.get_proxy_object("org.bluez", dev_path, dintro)
                dev1 = dobj.get_interface("org.bluez.Device1")
                props = dobj.get_interface("org.freedesktop.DBus.Properties")
                try:
                    await asyncio.wait_for(dev1.call_pair(), 25)
                    _LOG.warning("compass: Device1.Pair() succeeded")
                except Exception as e:  # noqa: BLE001
                    _LOG.warning("compass: Device1.Pair() failed: %s", e)
                try:
                    await props.call_set("org.bluez.Device1", "Trusted", Variant("b", True))
                except Exception:  # noqa: BLE001
                    pass
                try:
                    p = await props.call_get("org.bluez.Device1", "Paired")
                    ok = bool(getattr(p, "value", False))
                except Exception:  # noqa: BLE001
                    ok = False
                _LOG.warning("compass: pairing result paired=%s", ok)
                return ok
            finally:
                if registered and am is not None:
                    try:
                        await am.call_unregister_agent(agent_path)
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    bus.unexport(agent_path)
                except Exception:  # noqa: BLE001
                    pass
                bus.disconnect()

    async def _session_open(self, c, aa, tgt):
        # bound the notify-frame buffer on a long-lived warm link (safe: no _wait active yet)
        if len(self._frames) > 400:
            self._frames.clear()
        # Sessions are per-device with no global limit -- opening one for a new device never
        # conflicts with sessions we hold on OTHER devices, so there's no handoff to do. If this
        # device already has a hot session, this just refreshes it. A failed open here is RF
        # frame loss (lost seed/key), not a conflict, so we simply retry.
        for attempt in range(3):
            # Re-assert presence immediately before the request. On a long-lived warm link a
            # periodic heartbeat write can get starved and the gateway then drops our presence
            # and ignores directed requests -- this guarantees a fresh presence frame first.
            # Writes are without-response (like the app) so the handshake isn't 5 blocking ATT
            # round-trips -- just the two notify waits below -- which halves first-command latency.
            try:
                await self._wr_nr(c, _a11(0, aa, bytes([0x00, PROTO_VER]) + self.mac))
            except Exception:  # noqa: BLE001
                pass
            await self._wr_nr(c, _a29(128, aa, tgt, 66, b"\x00\x04"))  # seed request
            r = await self._wait(aa, 66, 1.5)
            if r is None:
                _LOG.debug("compass session: no seed from 0x%02x (try %d)", tgt, attempt)
                continue
            seed = int.from_bytes(r[8:12], "big")
            await self._wr_nr(c, _a29(128, aa, tgt, 67,
                                      b"\x00\x04" + _encrypt(REMOTE_CTRL_CYPHER, seed).to_bytes(4, "big")))
            k = await self._wait(aa, 67, 1.5)
            if k is not None and k[5] == 2:
                return True
            _LOG.debug("compass session: key not confirmed 0x%02x (try %d, k=%s)",
                       tgt, attempt, k.hex() if k else None)
        return False

    async def _open_and_learn(self, listen=2.5):
        self._frames.clear()
        self._devmap.clear()
        self._inuse.clear()
        c = await self._connect()
        if not await self._unlock(c):
            try:
                await c.disconnect()
            except Exception:
                pass
            raise RuntimeError("KeySeed unlock failed")
        await c.start_notify(CH_OUT, self._cb)
        await asyncio.sleep(listen)
        return c

    async def async_enumerate(self, listen=2.5):
        async with self._lock:
            if self._released:
                # We've handed the console to the phone app -- do NOT grab the connection.
                # Entities go unavailable until the user reclaims it.
                return {}
            self._ensure_supervisor()
            # Persistent (always-on) link: establish or reuse the warm connection and keep
            # it. The notify callback keeps the device map live, so polling never needs a
            # throwaway connect/disconnect and user actions never pay the cold-start cost.
            await self._ensure_warm()
            devs = dict(self._devmap)
            if devs:
                if _LOG.isEnabledFor(logging.DEBUG):
                    for a, d in sorted(devs.items()):
                        _LOG.debug("compass device 0x%02x type=%s(%s) func=%s(%s) key=%s pid=0x%04x "
                                   "status=%s", a, d.dtype, d.type_name, d.func, d.name, d.key,
                                   d.product_id, d.status.hex())
            return devs

    def _resolve(self, spec):
        if isinstance(spec, int):
            return spec
        s = str(spec).strip().lower()
        if s.startswith("0x"):
            try:
                return int(s, 16)
            except Exception:
                return None
        if s.startswith("key:"):
            want = s[4:]
            for a, d in self._devmap.items():
                if d.key == want:
                    return a
            return None
        fid = None
        if s.startswith("func:"):
            try:
                fid = int(s.split(":")[1])
            except Exception:
                return None
        elif s in ALIASES:
            fid = ALIASES[s]
        if fid is not None:
            for a, d in self._devmap.items():
                if d.func == fid:
                    return a
            return None
        for a, d in self._devmap.items():
            if s and s in d.name.lower().replace(" ", ""):
                return a
        return None

    def async_stop(self, key):
        """Signal a running async_move (for this device key) to stop."""
        ev = self._stop_events.get(key)
        if ev is not None:
            ev.set()

    def async_stop_all(self):
        """Emergency stop: signal every in-flight move to halt at once. The motors are
        momentary, so they stop the instant the pulse stream stops (each move also sends an
        explicit STOP as it breaks). Returns the number of moves that were running."""
        events = list(self._stop_events.values())
        for ev in events:
            ev.set()
        return len(events)

    async def async_clear_fault(self, key):
        """Clear a cover's OutputDisabledLatch via command mode 3 (the same 'clear fault' the
        OneControl panel sends) -- no power cycle needed. Returns True if the command was sent."""
        async with self._lock:
            try:
                c, aa = await self._ensure_warm()
                addr = self._resolve(f"key:{key}")
                if addr is None or addr not in self._devmap:
                    return False
                if not self._session_warm(addr):
                    if not await self._session_open(c, aa, addr):
                        raise RuntimeError("RemoteControl session did not open")
                self._mark_session(addr)
                ext, payload = P.clear_fault_cmd(self._devmap[addr].dtype)
                await self._wr(c, _a29(130, aa, addr, ext, payload))  # clear the fault latch
                self._touch_idle()
                return True
            except Exception:  # noqa: BLE001
                await self._teardown_warm()
                raise

    async def async_clear_all_faults(self):
        """Clear the latch on every cover currently reporting a fault. Returns the count."""
        faulted = [d.key for d in self._devmap.values() if getattr(d, "is_faulted", False)]
        n = 0
        for k in faulted:
            try:
                if await self.async_clear_fault(k):
                    n += 1
            except Exception:  # noqa: BLE001
                pass
        return n

    async def async_move(self, key, direction, seconds, max_seconds=40.0,
                         stall_amps=0.0, stall_hold=0.6, stall_arm=3.0, until=None):
        """Drive a momentary H-bridge (slide/awning) for ``seconds`` (or until stopped / link drop).

        If ``stall_amps`` > 0, also stop as soon as the motor current stays at/above that many
        amps for ``stall_hold`` s (after ``stall_arm`` s of run) -- i.e. it hit the physical
        end-stop. Returns ``(elapsed_seconds, stalled)``.

        direction: 1=extend/open, 2=retract/close. Returns the seconds actually run so the caller
        can estimate position from run-time. ``until`` is an optional callable checked every
        pulse; the move ends as soon as it returns True.

        Two things keep the motor moving smoothly:
          * a fast write-without-response movement pulse every 0.2s (the momentary motor stalls if
            it is not fed frequently), and
          * a RemoteControl session heartbeat every 2.5s -- the session otherwise times out after
            5s and the gateway then ignores commands mid-travel (the "intermittent/laggy" stall).
        """
        import time as _time
        run_for = max(0.0, min(float(seconds), float(max_seconds)))
        stop = asyncio.Event()
        self._stop_events[key] = stop
        elapsed = 0.0
        stalled = False
        try:
            async with self._lock:
                try:
                    c, aa = await self._ensure_warm()
                    addr = self._resolve(f"key:{key}")
                    if addr is None or addr not in self._devmap:
                        raise RuntimeError(f"cover '{key}' not found on bus")
                    # Reuse this cover's hot session if it's still warm (the heartbeat keeps it
                    # tended); otherwise open one. Sessions are per-device with no global limit,
                    # so this never conflicts with sessions held on other devices -- a failed
                    # open is just RF frame loss, so retry once. Never tear the link down for a
                    # session miss (that risks the KeySeed unlock and wedges the whole link).
                    if not self._session_warm(addr):
                        if not await self._session_open(c, aa, addr):
                            if not await self._session_open(c, aa, addr):
                                raise RuntimeError("RemoteControl session did not open")
                    self._mark_session(addr)  # keep this cover hot for an instant next command
                    dev = self._devmap[addr]
                    m_ext, m_pl = P.move_cmd(dev.dtype, direction, dev.fclass, FN.get(dev.func, ""))
                    s_ext, s_pl = P.move_cmd(dev.dtype, MOVE_STOP)
                    t0 = _time.monotonic()
                    end = t0 + run_for
                    last_hb = t0
                    stall_since = None
                    while not stop.is_set():
                        now = _time.monotonic()
                        if now >= end:
                            break
                        if until is not None and until():
                            break
                        if now - last_hb >= 2.5:
                            try:
                                await self._wr_nr(c, _a29(128, aa, addr, 68, b"\x00\x04"))  # keep session alive
                            except Exception:  # noqa: BLE001
                                await self._teardown_warm()
                                break
                            last_hb = now
                        # End-stop detection: sustained high current = hit the physical limit.
                        if stall_amps > 0 and (now - t0) >= stall_arm:
                            _d = self._devmap.get(addr)
                            cur = (_d.current if _d is not None else None) or 0.0
                            if cur >= stall_amps:
                                if stall_since is None:
                                    stall_since = now
                                elif now - stall_since >= stall_hold:
                                    stalled = True
                                    break
                            else:
                                stall_since = None
                        self._mark_session(addr)  # don't let the heartbeat END it mid-travel
                        try:
                            await self._wr_nr(c, _a29(130, aa, addr, m_ext, m_pl))  # movement pulse
                        except Exception:  # noqa: BLE001
                            await self._teardown_warm()  # link dropped -> motor stops itself
                            break
                        await asyncio.sleep(0.2)
                    elapsed = _time.monotonic() - t0
                    try:
                        if self._conn is not None:
                            await self._wr(c, _a29(130, aa, addr, s_ext, s_pl))
                    except Exception:  # noqa: BLE001
                        pass
                    self._mark_session(addr)  # stay hot so an immediate reverse is instant
                    self._touch_idle()
                    return elapsed, stalled
                except Exception:
                    # Only reset the link if it's actually dead. A session refusal ("did not
                    # open") leaves a HEALTHY link -- tearing it down here forced a reconnect
                    # whose KeySeed unlock then failed and took every entity unavailable.
                    try:
                        dead = self._conn is None or not self._conn.is_connected
                    except Exception:  # noqa: BLE001
                        dead = True
                    if dead:
                        await self._teardown_warm()
                    raise
        finally:
            self._stop_events.pop(key, None)

    async def async_set(self, spec, on):
        """Turn a latching relay on/off, retrying on transient BLE contention. (ok, Device|None)."""
        async with self._lock:
            last = None
            for outer in range(3):
                try:
                    return await self._attempt_set(spec, on)
                except Exception as e:  # noqa: BLE001
                    last = e
                    _LOG.debug("compass: toggle attempt %d failed: %s", outer, e)
                    # Only reconnect on a genuine link loss. If the link is healthy (the console
                    # just refused the session because one is still active), keep it and wait for
                    # that session to lapse -- tearing down risks a KeySeed-unlock cascade.
                    try:
                        dead = self._conn is None or not self._conn.is_connected
                    except Exception:  # noqa: BLE001
                        dead = True
                    if dead:
                        await self._teardown_warm()
                        await asyncio.sleep(1.0)
                    else:
                        await asyncio.sleep(3.0)
            raise last if last else RuntimeError("toggle failed")

    async def _attempt_set(self, spec, on):
        c, aa = await self._ensure_warm()
        addr = self._resolve(spec)
        if addr is None or addr not in self._devmap:
            raise RuntimeError(
                f"device '{spec}' not found on bus (seen: {[hex(a) for a in sorted(self._devmap)]})")
        if not self._session_warm(addr):
            # No warm session for this device -> open one (seed/key handshake, ~1s).
            if not await self._session_open(c, aa, addr):
                raise RuntimeError("RemoteControl session did not open")
        self._mark_session(addr)
        ext, payload = P.relay_cmd(self._devmap[addr].dtype, on)
        await self._wr(c, _a29(130, aa, addr, ext, payload))  # command
        self._mark_session(addr)  # keep this device hot for an instant next command
        # Brief best-effort confirm. Relays re-broadcast promptly when a load appears (ON)
        # but go quiet when it drops (OFF), so don't block on it -- the switch entity is
        # optimistic and reconciles from the device's own broadcasts.
        ok = False
        for _ in range(12):
            await asyncio.sleep(0.1)
            if self._devmap[addr].is_on == on:
                ok = True
                break
        _LOG.debug("compass: 0x%02x on=%s ok=%s state=0x%02x", addr, on, ok, self._devmap[addr].state)
        self._touch_idle()
        return ok, self._devmap.get(addr)

    async def async_command(self, key, command, confirm=None, timeout=3.0):
        """Send one device command ``(command_byte, data)`` (see protocol.*_cmd) to the device
        with this key. Like the app's command runner, it re-sends every ~250 ms until
        ``confirm(device)`` is True or ``timeout`` expires. Returns True if confirmed (or sent,
        when no confirm callable is given)."""
        ext, payload = command
        async with self._lock:
            last = None
            for _attempt in range(2):
                try:
                    c, aa = await self._ensure_warm()
                    addr = self._resolve(f"key:{key}")
                    if addr is None or addr not in self._devmap:
                        raise RuntimeError(f"device '{key}' not found on bus")
                    if not self._session_warm(addr):
                        if not await self._session_open(c, aa, addr):
                            raise RuntimeError("RemoteControl session did not open")
                    self._mark_session(addr)
                    frame = _a29(130, aa, addr, ext, payload)
                    await self._wr(c, frame)
                    if confirm is None:
                        self._touch_idle()
                        return True
                    deadline = time.monotonic() + timeout
                    while time.monotonic() < deadline:
                        await asyncio.sleep(0.25)
                        dev = self._devmap.get(addr)
                        if dev is not None and confirm(dev):
                            self._touch_idle()
                            return True
                        self._mark_session(addr)
                        await self._wr_nr(c, frame)
                    self._touch_idle()
                    return False
                except Exception as e:  # noqa: BLE001
                    last = e
                    try:
                        dead = self._conn is None or not self._conn.is_connected
                    except Exception:  # noqa: BLE001
                        dead = True
                    if dead:
                        await self._teardown_warm()
                    await asyncio.sleep(1.0)
            raise last if last else RuntimeError("command failed")


if __name__ == "__main__":
    import sys

    async def _main():
        # usage: python -m compass_ble <MAC> [list | <device> <on|off>]
        if len(sys.argv) < 2:
            sys.exit("usage: compass_ble.py <console MAC> [list | <device> <on|off>]")
        cli = CompassClient(sys.argv[1])
        args = sys.argv[2:]
        if not args or args[0] == "list":
            for a, d in sorted((await cli.async_enumerate()).items()):
                tag = "switch" if d.is_switch else "      "
                st = ("ON" if d.is_on else "OFF") if d.is_switch else f"0x{d.state:02x}"
                print(f"  0x{a:02x}  {d.name:28s} [{d.type_name:22s}] {tag} {st}")
        else:
            on = args[1].lower() == "on" if len(args) > 1 else True
            ok, dev = await cli.async_set(args[0], on)
            print(">>> OK" if ok else ">>> FAILED", "->", dev)

    asyncio.run(_main())
