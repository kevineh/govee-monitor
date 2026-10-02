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
    H5055_MFG_ID,
    ChannelReading,
    DecodeError,
    DecodedPayload,
    decode_advertisement,
    decode_payload,
)
from .state import DeviceState

log = logging.getLogger(__name__)


class H5055Scanner:
    """Continuous bleak scanner: one ``start()`` for the whole session.

    Advertisements are filtered by exact MAC (``--mac``) or the Govee prefix
    (default ``a4:c1:38``) plus the manufacturer key, decoded, and merged into
    the shared state.

    Measured device behaviour (see the notes in ``docs``/memory): the H5055
    emits short *pairs* of advertisements at ~1 s cadence in aggregate, but the
    ``payload_index`` rotates over three probe pairs, so the pair carrying a
    given channel is only seen every few seconds. Windows also delivers many
    byte-identical duplicates. Two consequences shape this class:

    * a single long-lived scanner captures more than one that is restarted --
      re-arming the radio drops packets -- so restarts are a last-resort
      recovery only, not a steady-state strategy;
    * because each ``payload_index`` reports only two of the six channels, a
      channel can go stale for tens of seconds while traffic still flows, so
      the watchdog tracks the age of the *probe readings* separately from the
      age of the last advertisement.
    """

    def __init__(
        self,
        state: DeviceState,
        *,
        mac: str | None = None,
        mac_prefix: str = "a4:c1:38",
        scan_mode: str = "active",
        watchdog: float = 30.0,
        reading_watchdog: float = 60.0,
        dedupe_window: float = 0.25,
        restart_on_watchdog: bool = False,
    ) -> None:
        self.state = state
        self.mac = mac.lower() if mac else None
        self.mac_prefix = mac_prefix
        self.scan_mode = scan_mode
        self.watchdog = watchdog
        self.reading_watchdog = reading_watchdog
        self.dedupe_window = dedupe_window
        self.restart_on_watchdog = restart_on_watchdog
        self._scanner: object | None = None
        self._watchdog_task: asyncio.Task | None = None
        self._misses = 0
        self._last_sig: tuple | None = None
        self._last_sig_at = 0.0
        self._duplicates = 0
        self._started_at = time.monotonic()
        # monotonic time of the last advertisement that carried a real probe
        # reading, and the readings themselves, for change detection.
        self.last_reading_at: float | None = None
        self._last_readings: dict[int, float] = {}

    def _matches(self, address: str) -> bool:
        addr = address.lower()
        if self.mac:
            return addr == self.mac
        return addr.startswith(self.mac_prefix)

    def _on_detect(self, device, advertisement) -> None:
        if not self._matches(device.address):
            return
        try:
            payload = decode_advertisement(advertisement.manufacturer_data)
        except DecodeError as exc:
            mfg = b"".join(
                advertisement.manufacturer_data.get(key, b"")
                for key in (GOVEE_MFG_ID, H5055_MFG_ID)
            )
            log.warning(
                "decode error from %s: %s (mfg=%s)", device.address, exc, mfg.hex()
            )
            return
        if payload is None:
            return

        now = time.monotonic()

        # Windows (and the device's ADV/SCAN_RSP pair) deliver byte-identical
        # frames within milliseconds of each other. Recording them twice
        # inflates sample counts and makes an "update rate" look better than it
        # is, so collapse repeats inside ``dedupe_window``. A genuine repeat of
        # the same temperature later than the window is still recorded, which
        # keeps long stable plateaus visible in the logs.
        sig = (
            payload.payload_index,
            payload.channel_a.value,
            payload.channel_b.value,
            payload.battery,
        )
        if (
            self.dedupe_window > 0
            and sig == self._last_sig
            and now - self._last_sig_at < self.dedupe_window
        ):
            self._duplicates += 1
            # Still refresh liveness so the "any traffic" watchdog is accurate.
            self.state.last_seen = now
            self.state.rssi = advertisement.rssi
            return
        self._last_sig = sig
        self._last_sig_at = now

        # Track probe-reading freshness independently of general traffic: a
        # channel can be absent for a long time while other probe pairs (which
        # this device reports as 0xFFFF) keep arriving.
        readings = {
            ch: r.value
            for ch, r in (
                (payload.base_channel, payload.channel_a),
                (payload.base_channel + 1, payload.channel_b),
            )
            if r.value is not None
        }
        if readings:
            self.last_reading_at = now
            self._last_readings = readings

        self.state.update(payload, advertisement.rssi, now)

    async def start(self) -> None:
        log.info(
            "starting H5055 scan (mac=%s scan_mode=%s watchdog=%.0fs "
            "reading_watchdog=%.0fs dedupe=%.2fs)",
            self.mac or self.mac_prefix + "*",
            self.scan_mode,
            self.watchdog,
            self.reading_watchdog,
            self.dedupe_window,
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
            now = time.monotonic()

            # 1) Is the device audible at all? This catches a dead adapter or a
            #    device that went out of range.
            last = self.state.last_seen
            age = now - last if last is not None else self.watchdog + 1
            if age > self.watchdog:
                self._misses += 1
                log.warning(
                    "no H5055 advertisements for %.0fs (miss %d)", age, self._misses
                )
                if self.restart_on_watchdog and self._misses >= 3:
                    await self.restart()
            else:
                self._misses = 0

            # 2) Are the *probes* still reporting? Because payload_index rotates
            #    over three pairs, traffic keeps flowing while a given channel
            #    is silent, so this is a separate and usually more informative
            #    signal than the liveness check above.
            if self.reading_watchdog > 0:
                seen = self.last_reading_at
                if seen is None:
                    log.warning(
                        "no probe readings yet (%.0fs since start); "
                        "check that a probe is plugged in",
                        now - self._started_at,
                    )
                elif now - seen > self.reading_watchdog:
                    log.warning(
                        "no probe readings for %.0fs (last: %s)",
                        now - seen,
                        ", ".join(
                            f"ch{c}={v}" for c, v in sorted(self._last_readings.items())
                        )
                        or "none",
                    )


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
        try:
            payload = decode_advertisement(advertisement.manufacturer_data)
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
        if payload is None:
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
