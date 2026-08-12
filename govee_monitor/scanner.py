"""BLE scanning and simulation sources that feed :class:`DeviceState`.

The H5055 broadcasts advertisements continuously -- no connect/pair/GATT is
needed. bleak is imported lazily so importing this module never requires a
working BLE stack (unit tests on non-BLE machines can still run the sim).
"""

from __future__ import annotations

import asyncio
import logging
import time

from .decoder import (
    DISCONNECTED,
    GOVEE_MFG_ID,
    ChannelReading,
    DecodeError,
    DecodedPayload,
    decode_payload,
)
from .state import DeviceState

log = logging.getLogger(__name__)


class H5055Scanner:
    """Continuous bleak scanner: one ``start()`` for the whole session.

    Advertisements are filtered by exact MAC (``--mac``) or the Govee prefix
    (default ``a4:c1:38``) plus the 0xEC88 manufacturer key, decoded, and
    merged into the shared state. A watchdog warns when no advertisement has
    arrived for ``watchdog`` seconds and, if enabled, restarts the scan.
    """

    def __init__(
        self,
        state: DeviceState,
        *,
        mac: str | None = None,
        mac_prefix: str = "a4:c1:38",
        scan_mode: str = "active",
        watchdog: float = 30.0,
        restart_on_watchdog: bool = False,
    ) -> None:
        self.state = state
        self.mac = mac.lower() if mac else None
        self.mac_prefix = mac_prefix
        self.scan_mode = scan_mode
        self.watchdog = watchdog
        self.restart_on_watchdog = restart_on_watchdog
        self._scanner: object | None = None
        self._watchdog_task: asyncio.Task | None = None
        self._misses = 0

    def _matches(self, address: str) -> bool:
        addr = address.lower()
        if self.mac:
            return addr == self.mac
        return addr.startswith(self.mac_prefix)

    def _on_detect(self, device, advertisement) -> None:
        if not self._matches(device.address):
            return
        mfg = advertisement.manufacturer_data.get(GOVEE_MFG_ID)
        if mfg is None:
            return
        try:
            payload = decode_payload(mfg)
        except DecodeError as exc:
            log.warning(
                "decode error from %s: %s (mfg=%s)", device.address, exc, mfg.hex()
            )
            return
        self.state.update(payload, advertisement.rssi, time.monotonic())

    async def start(self) -> None:
        log.info(
            "starting H5055 scan (mac=%s scan_mode=%s watchdog=%.0fs)",
            self.mac or self.mac_prefix + "*",
            self.scan_mode,
            self.watchdog,
        )
        from bleak import BleakScanner  # lazy: only needed for real BLE

        self._scanner = BleakScanner(
            detection_callback=self._on_detect, scanning_mode=self.scan_mode
        )
        await self._scanner.start()
        if self.watchdog > 0:
            self._watchdog_task = asyncio.create_task(self._watchdog_loop())

    async def stop(self) -> None:
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            self._watchdog_task = None
        if self._scanner is not None:
            await self._scanner.stop()
            self._scanner = None

    async def restart(self) -> None:
        log.warning("restarting H5055 scanner")
        await self.stop()
        await self.start()

    async def _watchdog_loop(self) -> None:
        while True:
            await asyncio.sleep(self.watchdog)
            last = self.state.last_seen
            age = time.monotonic() - last if last is not None else self.watchdog + 1
            if age > self.watchdog:
                self._misses += 1
                log.warning(
                    "no H5055 advertisements for %.0fs (miss %d)", age, self._misses
                )
                if self.restart_on_watchdog and self._misses >= 3:
                    await self.restart()
            else:
                self._misses = 0


async def list_h5055(
    mac: str | None = None,
    mac_prefix: str = "a4:c1:38",
    duration: float = 5.0,
) -> list[dict]:
    """Scan briefly and collect info about discovered H5055 advertisements."""

    from bleak import BleakScanner  # lazy

    found: dict[str, dict] = {}

    def cb(device, advertisement) -> None:
        addr = device.address.lower()
        if mac:
            if addr != mac.lower():
                return
        elif not addr.startswith(mac_prefix):
            return
        mfg = advertisement.manufacturer_data.get(GOVEE_MFG_ID)
        if mfg is None:
            return
        try:
            payload = decode_payload(mfg)
        except DecodeError as exc:
            found[addr] = {
                "name": device.name,
                "mac": addr,
                "rssi": advertisement.rssi,
                "battery": None,
                "mask": None,
                "channels": None,
                "note": f"decode error: {exc}",
            }
            return
        found[addr] = {
            "name": device.name,
            "mac": addr,
            "rssi": advertisement.rssi,
            "battery": payload.battery,
            "mask": f"{payload.connection_mask:06b}",
            "channels": list(payload.connected_channels),
            "note": None,
        }

    scanner = BleakScanner(detection_callback=cb)
    await scanner.start()
    await asyncio.sleep(duration)
    await scanner.stop()
    return list(found.values())


class SimSource:
    """Synthetic H5055 advertisements to exercise the pipeline without BLE.

    Builds real byte patterns (as the device would broadcast them) with the
    configured sensors connected, then feeds them through ``decode_payload``
    so the whole decode chain is exercised. Bean temp (BT) ramps from
    ``start_temp`` to ``end_temp``; exhaust temp (ET) trails by ``et_offset``.
    """

    def __init__(
        self,
        state: DeviceState,
        *,
        connected: tuple[int, ...] | None = None,
        start_temp: float = 100.0,
        end_temp: float = 200.0,
        et_offset: float = 25.0,
        ramp_steps: int = 60,
        battery: int = 88,
        interval: float = 1.0,
        max_steps: int | None = None,
    ) -> None:
        self.state = state
        if connected is None:
            connected = tuple(
                ch for ch in (state.bt_channel, state.et_channel) if ch is not None
            ) or (4, 6)
        self.connected = set(connected)
        self.start_temp = start_temp
        self.end_temp = end_temp
        self.et_offset = et_offset
        self.ramp_steps = ramp_steps
        self.battery = battery
        self.interval = interval
        self.max_steps = max_steps
        self._mask = sum(1 << (ch - 1) for ch in self.connected)
        self._step = 0
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def wait_done(self) -> None:
        if self._task is not None:
            await self._task

    def _build_bytes(self, payload_index: int, bt: float, et: float) -> bytes:
        """Build a real 22-byte payload for one sensor pair.

        Each slot is filled according to the channel it represents: the BT
        channel carries ``bt``, the ET channel carries ``et``, any other
        slot reports disconnected.
        """
        temps = {}
        if self.state.bt_channel is not None:
            temps[self.state.bt_channel] = round(bt)
        if self.state.et_channel is not None:
            temps[self.state.et_channel] = round(et)
        base = 2 * payload_index + 1
        a_ch, b_ch = base, base + 1
        raw = bytearray(22)
        raw[0] = 0xCF
        raw[1] = 0x04
        raw[2] = 0x04
        raw[3] = 0x00
        raw[4] = self.battery
        raw[5] = (payload_index << 6) | self._mask
        raw[6] = 0x06
        a_value = temps.get(a_ch, DISCONNECTED)
        b_value = temps.get(b_ch, DISCONNECTED)
        raw[7:9] = a_value.to_bytes(2, "little")
        raw[9:11] = DISCONNECTED.to_bytes(2, "little")  # low alarm unset
        raw[11:13] = (300).to_bytes(2, "little")  # high alarm 300
        raw[14:16] = b_value.to_bytes(2, "little")
        raw[16:18] = DISCONNECTED.to_bytes(2, "little")
        raw[18:20] = (300).to_bytes(2, "little")
        raw[20] = 0x00
        raw[21] = 0x00
        return bytes(raw)

    async def _run(self) -> None:
        while True:
            if self.max_steps is not None and self._step >= self.max_steps:
                log.info("sim finished after %d steps", self._step)
                break
            progress = self._step / max(1, self.ramp_steps - 1)
            progress = min(1.0, progress)
            bt = self.start_temp + progress * (self.end_temp - self.start_temp)
            et = max(0.0, bt - self.et_offset)
            for idx in range(3):  # rotate through all three sensor pairs
                payload = decode_payload(self._build_bytes(idx, bt, et))
                self.state.update(payload, -70, time.monotonic())
            self._step += 1
            await asyncio.sleep(self.interval)
