"""Pure decoding of Govee H5055 BLE advertisement payloads.

The byte layout below was reverse-engineered in
https://github.com/BastiJoe/govee_h5055_mqtt. Each advertisement carries
only ONE sensor pair (the payload_index in the top bits of the channel byte
selects which pair); the caller must keep feeding all three payload
variants to assemble the full 6-channel picture.

Offsets are given as byte indices here. They map 1:1 to the reference
repo's hex-string slices (2 hex chars == 1 byte):

    battery           mfg[4]
    channel byte      mfg[5]
    A-slot value      mfg[7:9]  little-endian   (hex [16:18]+[14:16])
    A-slot low alarm  mfg[9:11]                 (hex [20:22]+[18:20])
    A-slot high alarm mfg[11:13]                (hex [24:26]+[22:24])
    B-slot value      mfg[14:16]                (hex [30:32]+[28:30])
    B-slot low alarm  mfg[16:18]                (hex [34:36]+[32:34])
    B-slot high alarm mfg[18:20]                (hex [38:40]+[36:38])

Windows bleak already strips the 2-byte company ID (0xEC88), so the payload
starts directly at byte 0. We defensively strip it again if a backend left
it in place.

0xFFFF means the slot has no probe connected / no value set.
"""

from __future__ import annotations

import dataclasses

GOVEE_MFG_ID = 0xEC88
MAC_PREFIX = "a4:c1:38"
DISCONNECTED = 0xFFFF
MAX_TEMP = 300  # raw integer °C reading; above this (and != 0xFFFF) is suspect


class DecodeError(ValueError):
    """Raised when an advertisement payload fails sanity checks."""


@dataclasses.dataclass(frozen=True)
class ChannelReading:
    """A single sensor slot: value plus the configured alarm thresholds."""

    value: float | None
    low_alarm: int | None
    high_alarm: int | None


@dataclasses.dataclass(frozen=True)
class DecodedPayload:
    battery: int
    payload_index: int
    base_channel: int
    connection_mask: int
    channel_a: ChannelReading
    channel_b: ChannelReading

    @property
    def connected_channels(self) -> tuple[int, ...]:
        """Sensor numbers (1..6) whose probes are plugged in, per the mask."""
        return tuple(i + 1 for i in range(6) if self.connection_mask & (1 << i))


def _strip_company_id(mfg: bytes) -> bytes:
    """Windows bleak strips 0xEC88; strip it ourselves if a backend left it."""
    if len(mfg) >= 24 and mfg[:2] in (b"\x88\xec", b"\xec\x88"):
        return mfg[2:]
    return mfg


def _read_u16(mfg: bytes, low: int, high: int) -> int:
    """Little-endian u16 from two byte positions (mirrors the repo's swap)."""
    return mfg[low] | (mfg[high] << 8)


def _check_range(value: int, name: str) -> None:
    if value != DISCONNECTED and not 0 <= value <= MAX_TEMP:
        raise DecodeError(f"{name} {value} out of range 0..{MAX_TEMP} or 0xFFFF")


def _reading(value: int, low_alarm: int, high_alarm: int) -> ChannelReading:
    _check_range(value, "temperature")
    _check_range(low_alarm, "low alarm")
    _check_range(high_alarm, "high alarm")
    return ChannelReading(
        value=None if value == DISCONNECTED else float(value),
        low_alarm=None if low_alarm == DISCONNECTED else low_alarm,
        high_alarm=None if high_alarm == DISCONNECTED else high_alarm,
    )


def decode_payload(mfg_bytes: bytes) -> DecodedPayload:
    """Decode a manufacturer-data payload.

    Raises :class:`DecodeError` if the payload fails any sanity check. The
    caller should log the raw hex of a failing payload for diagnosis.
    """
    mfg = _strip_company_id(mfg_bytes)
    if len(mfg) < 20:
        raise DecodeError(f"payload too short: {len(mfg)} bytes")

    battery = mfg[4]
    if battery > 100:
        raise DecodeError(f"battery {battery} out of range 0..100")

    channel_byte = mfg[5]
    payload_index = (channel_byte >> 6) & 0x03
    if payload_index > 2:
        raise DecodeError(f"payload_index {payload_index} out of range 0..2")
    base_channel = 2 * payload_index + 1
    connection_mask = channel_byte & 0x3F

    channel_a = _reading(
        _read_u16(mfg, 7, 8),
        _read_u16(mfg, 9, 10),
        _read_u16(mfg, 11, 12),
    )
    channel_b = _reading(
        _read_u16(mfg, 14, 15),
        _read_u16(mfg, 16, 17),
        _read_u16(mfg, 18, 19),
    )

    return DecodedPayload(
        battery=battery,
        payload_index=payload_index,
        base_channel=base_channel,
        connection_mask=connection_mask,
        channel_a=channel_a,
        channel_b=channel_b,
    )
