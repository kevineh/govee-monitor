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

Newer H5055 firmware broadcasts a different 20-byte payload under
manufacturer ID 0x0070 (big-endian, values in hundredths of a degree):

    battery           mfg[5] & 0x7F
    channel byte      mfg[6]  (payload_index / connection_mask as above)
    A-slot value      mfg[8:10]  big-endian /100   (0xFFFF = no probe)
    A-slot low alarm  mfg[10:12]
    A-slot high alarm mfg[12:14]
    B-slot value      mfg[14:16]
    B-slot low alarm  mfg[16:18]
    B-slot high alarm mfg[18:20]
"""

from __future__ import annotations

import dataclasses

GOVEE_MFG_ID = 0xEC88
H5055_MFG_ID = 0x0070
MAC_PREFIX = "a4:c1:38"
DISCONNECTED = 0xFFFF
MAX_TEMP = 300  # raw integer °C reading; above this (and != 0xFFFF) is suspect
MAX_TEMP_RAW_NEW = 30000  # new format: raw hundredths of a degree (300.00 °C)


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


def _read_u16_be(mfg: bytes, lo: int, hi: int) -> int:
    """Big-endian u16 from two byte positions (new-firmware format)."""
    return (mfg[lo] << 8) | mfg[hi]


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


def _new_reading(value_raw: int, low_raw: int, high_raw: int) -> ChannelReading:
    """New-format slot: big-endian raw values, temperature in hundredths of °C."""
    if value_raw != DISCONNECTED and not 0 <= value_raw <= MAX_TEMP_RAW_NEW:
        raise DecodeError(
            f"temperature {value_raw} out of range 0..{MAX_TEMP_RAW_NEW} or 0xFFFF"
        )
    return ChannelReading(
        value=None if value_raw == DISCONNECTED else value_raw / 100.0,
        low_alarm=None if low_raw == DISCONNECTED else low_raw,
        high_alarm=None if high_raw == DISCONNECTED else high_raw,
    )


def decode_payload_new(mfg_bytes: bytes) -> DecodedPayload:
    """Decode a 20-byte new-firmware H5055 manufacturer-data payload (mfg 0x0070).

    The byte layout is documented at the top of this module. Values are
    big-endian and temperatures are already scaled to °C (raw / 100), so no
    ``--temp-divisor`` is needed for this format.
    """
    mfg = _strip_company_id(mfg_bytes)
    if len(mfg) != 20:
        raise DecodeError(f"new-format payload must be 20 bytes, got {len(mfg)}")

    battery = mfg[5] & 0x7F
    if battery > 100:
        raise DecodeError(f"battery {battery} out of range 0..100")

    channel_byte = mfg[6]
    payload_index = (channel_byte >> 6) & 0x03
    if payload_index > 2:
        raise DecodeError(f"payload_index {payload_index} out of range 0..2")
    base_channel = 2 * payload_index + 1
    connection_mask = channel_byte & 0x3F

    channel_a = _new_reading(
        _read_u16_be(mfg, 8, 9),
        _read_u16_be(mfg, 10, 11),
        _read_u16_be(mfg, 12, 13),
    )
    channel_b = _new_reading(
        _read_u16_be(mfg, 14, 15),
        _read_u16_be(mfg, 16, 17),
        _read_u16_be(mfg, 18, 19),
    )

    return DecodedPayload(
        battery=battery,
        payload_index=payload_index,
        base_channel=base_channel,
        connection_mask=connection_mask,
        channel_a=channel_a,
        channel_b=channel_b,
    )


def decode_advertisement(mfg_data: dict[int, bytes]) -> DecodedPayload | None:
    """Decode the first recognizable Govee H5055 payload in ``mfg_data``.

    Old firmware uses manufacturer ID ``0xEC88``; new firmware uses ``0x0070``.
    Returns ``None`` when neither key is present (not an H5055, ignore
    silently).
    """
    if GOVEE_MFG_ID in mfg_data:
        return decode_payload(mfg_data[GOVEE_MFG_ID])
    if H5055_MFG_ID in mfg_data:
        return decode_payload_new(mfg_data[H5055_MFG_ID])
    return None
