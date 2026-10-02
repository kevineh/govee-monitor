"""Scanner tests for duplicate collapsing and probe-reading freshness.

The H5055 emits short pairs of advertisements, and Windows delivers many
byte-identical frames within milliseconds. Measured behaviour showed that
roughly half of all callbacks are such duplicates, so the scanner must collapse
them without losing the ability to see a genuinely repeated reading later.

These tests drive ``H5055Scanner._on_detect`` directly with fake bleak objects,
so they need no Bluetooth adapter.
"""

from types import SimpleNamespace

from govee_monitor.decoder import H5055_MFG_ID
from govee_monitor.scanner import H5055Scanner
from govee_monitor.state import DeviceState

ADDR = "a4:c1:38:70:00:83"

# Real capture: new firmware, 25.00 C on sensor 1, battery 100, payload_index 0.
FRAME_CH1 = bytes.fromhex("8341000101e4010009c4ffffffffffffffffffff")
# Same device, payload_index 2 (sensors 5/6), both slots empty.
FRAME_EMPTY = bytes.fromhex("8341000101e48100ffffffffffffffffffffffff")


def _advert(payload: bytes, rssi: int = -50):
    return SimpleNamespace(manufacturer_data={H5055_MFG_ID: payload}, rssi=rssi)


def _device(address: str = ADDR):
    return SimpleNamespace(address=address)


def _scanner(**kwargs) -> H5055Scanner:
    kwargs.setdefault("dedupe_window", 0.25)
    return H5055Scanner(DeviceState(bt_channel=1, et_channel=2), **kwargs)


def test_duplicate_frames_within_window_are_collapsed():
    s = _scanner()
    s._on_detect(_device(), _advert(FRAME_CH1))
    s._on_detect(_device(), _advert(FRAME_CH1))

    assert s._duplicates == 1
    # The reading is still present exactly once, at the right value.
    assert s.state.channels[1] == 25.0


def test_duplicate_still_refreshes_liveness():
    """A collapsed duplicate must keep the 'any traffic' watchdog happy."""
    s = _scanner()
    s._on_detect(_device(), _advert(FRAME_CH1))

    # Wipe liveness, then send a duplicate: it must be restored even though the
    # reading itself is collapsed.
    s.state.last_seen = 0.0
    s.state.rssi = None
    s._on_detect(_device(), _advert(FRAME_CH1, rssi=-42))

    assert s._duplicates == 1
    assert s.state.last_seen > 0.0
    assert s.state.rssi == -42


def test_same_reading_after_window_is_accepted():
    """A stable temperature must keep being recorded, not deduped forever."""
    s = _scanner()
    s._on_detect(_device(), _advert(FRAME_CH1))
    # Pretend the previous frame arrived a second ago.
    s._last_sig_at -= 1.0
    s._on_detect(_device(), _advert(FRAME_CH1))

    assert s._duplicates == 0


def test_dedupe_can_be_disabled():
    s = _scanner(dedupe_window=0.0)
    s._on_detect(_device(), _advert(FRAME_CH1))
    s._on_detect(_device(), _advert(FRAME_CH1))

    assert s._duplicates == 0


def test_reading_age_tracks_only_real_readings():
    """A frame whose slots are all empty must not refresh probe freshness.

    This is the signal that distinguishes 'device is talking' from 'the probes
    are actually reporting', which the payload rotation makes necessary.
    """
    s = _scanner()
    s._on_detect(_device(), _advert(FRAME_EMPTY))
    assert s.last_reading_at is None

    s._on_detect(_device(), _advert(FRAME_CH1))
    assert s.last_reading_at is not None
    assert s._last_readings == {1: 25.0}


def test_other_devices_are_ignored():
    s = _scanner()
    s._on_detect(_device("aa:bb:cc:dd:ee:ff"), _advert(FRAME_CH1))

    assert s.state.last_seen is None
    assert s.state.channels == {}


def test_mac_filter_matches_exactly():
    s = _scanner(mac=ADDR)
    s._on_detect(_device(ADDR.upper()), _advert(FRAME_CH1))
    assert s.state.channels.get(1) == 25.0

    t = _scanner(mac=ADDR)
    t._on_detect(_device("a4:c1:38:70:00:84"), _advert(FRAME_CH1))
    assert t.state.channels == {}
