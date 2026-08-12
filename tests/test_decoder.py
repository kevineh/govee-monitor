"""Decoder tests pinned to the two known H5055 sample frames."""

import pytest

from govee_monitor.decoder import (
    DISCONNECTED,
    GOVEE_MFG_ID,
    H5055_MFG_ID,
    MAC_PREFIX,
    DecodeError,
    decode_advertisement,
    decode_payload,
    decode_payload_new,
)

# Known good frames from the reference repo (old firmware, mfg 0xEC88).
SAMPLE_A = bytes.fromhex("cf040400464906ffffffff2c01061700ffff2c010000")
SAMPLE_B = bytes.fromhex("cf0404003b0806ffffffff2c0106ffffffff2c010000")

# Real captures from a new-firmware H5055 (mfg 0x0070, 20 bytes, big-endian).
# 25.00°C on sensor 1, battery 100, probe plugged into port 1.
NEW_FRAME_1 = bytes.fromhex("8341000101e4010009c4ffffffffffffffffffff")
# Same device, payload_index 2 (sensors 5/6), both slots empty.
NEW_FRAME_2 = bytes.fromhex("8341000101e48100ffffffffffffffffffffffff")


def test_sample_a_decodes_as_documented():
    d = decode_payload(SAMPLE_A)
    assert d.battery == 70  # 0x46
    assert d.payload_index == 1
    assert d.base_channel == 3
    assert d.connection_mask == 0b001001  # bits 0 and 3 -> sensors 1 and 4
    assert d.connected_channels == (1, 4)

    # Sensor 3 (A slot) has no probe -> 0xFFFF.
    assert d.channel_a.value is None
    assert d.channel_a.low_alarm is None
    assert d.channel_a.high_alarm == 300

    # Sensor 4 (B slot) reads 0x0017 == 23.
    assert d.channel_b.value == 23.0
    assert d.channel_b.high_alarm == 300


def test_sample_b_decodes_as_documented():
    d = decode_payload(SAMPLE_B)
    assert d.battery == 59  # 0x3B
    assert d.payload_index == 0
    assert d.base_channel == 1
    assert d.connection_mask == 0b001000  # only sensor 4
    assert d.connected_channels == (4,)

    # Neither slot carries a valid temperature (single-packet key case).
    assert d.channel_a.value is None
    assert d.channel_b.value is None


def test_disconnected_marker_constant():
    assert DISCONNECTED == 0xFFFF


def test_company_id_prefix_is_stripped_defensively():
    # A 24-byte payload with the little-endian company ID prefix decodes
    # identically to the already-stripped 22-byte payload.
    with_company = b"\x88\xec" + SAMPLE_A
    assert decode_payload(with_company) == decode_payload(SAMPLE_A)


def test_battery_above_100_raises():
    bad = bytearray(SAMPLE_A)
    bad[4] = 150
    with pytest.raises(DecodeError):
        decode_payload(bytes(bad))


def test_temperature_above_300_raises():
    bad = bytearray(SAMPLE_A)
    bad[15] = 0x34  # B-slot high byte -> 0x3417 = 13335 > 300
    with pytest.raises(DecodeError):
        decode_payload(bytes(bad))


def test_payload_index_3_raises():
    bad = bytearray(SAMPLE_A)
    bad[5] = (3 << 6) | 0x09  # top two bits 11 -> invalid payload_index
    with pytest.raises(DecodeError):
        decode_payload(bytes(bad))


def test_too_short_payload_raises():
    with pytest.raises(DecodeError):
        decode_payload(b"\x00\x00")


def test_constants_match_spec():
    assert GOVEE_MFG_ID == 0xEC88
    assert H5055_MFG_ID == 0x0070
    assert MAC_PREFIX == "a4:c1:38"


def test_new_frame_1_decodes_as_documented():
    d = decode_payload_new(NEW_FRAME_1)
    assert d.battery == 100  # mfg[5]=0xE4, top bit is a flag -> & 0x7F
    assert d.payload_index == 0
    assert d.base_channel == 1
    assert d.connection_mask == 0b000001  # sensor 1 plugged in
    assert d.connected_channels == (1,)

    # 0x09C4 big-endian == 2500 == 25.00°C.
    assert d.channel_a.value == 25.0
    assert d.channel_a.low_alarm is None
    assert d.channel_a.high_alarm is None

    # B slot empty (0xFFFF) -> None across the board.
    assert d.channel_b.value is None
    assert d.channel_b.low_alarm is None
    assert d.channel_b.high_alarm is None


def test_new_frame_2_decodes_as_documented():
    d = decode_payload_new(NEW_FRAME_2)
    assert d.battery == 100
    assert d.payload_index == 2
    assert d.base_channel == 5
    assert d.channel_a.value is None
    assert d.channel_b.value is None


def test_decode_advertisement_dispatches():
    # New firmware key -> new decoder.
    d_new = decode_advertisement({H5055_MFG_ID: NEW_FRAME_1})
    assert d_new is not None
    assert d_new.channel_a.value == 25.0

    # Old firmware key -> old decoder.
    d_old = decode_advertisement({GOVEE_MFG_ID: SAMPLE_A})
    assert d_old is not None
    assert d_old.battery == 70

    # Neither key -> not an H5055, silently ignored.
    assert decode_advertisement({}) is None
