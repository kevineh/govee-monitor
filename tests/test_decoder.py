"""Decoder tests pinned to the two known H5055 sample frames."""

import pytest

from govee_monitor.decoder import (
    DISCONNECTED,
    GOVEE_MFG_ID,
    MAC_PREFIX,
    DecodeError,
    decode_payload,
)

# Known good frames from the reference repo.
SAMPLE_A = bytes.fromhex("cf040400464906ffffffff2c01061700ffff2c010000")
SAMPLE_B = bytes.fromhex("cf0404003b0806ffffffff2c0106ffffffff2c010000")


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
    assert MAC_PREFIX == "a4:c1:38"
