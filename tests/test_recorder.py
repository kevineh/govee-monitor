"""Recorder tests: file rotation, JSONL/CSV content, and a sim-driven E2E run."""

import asyncio
import csv
import json

import pytest

from govee_monitor.events import CHARGE, DROP
from govee_monitor.recorder import Recorder
from govee_monitor.scanner import SimSource
from govee_monitor.state import DeviceState


def _state() -> DeviceState:
    st = DeviceState(bt_channel=4, et_channel=6)
    st.channels[4] = 198.0
    st.channels[6] = 185.5
    st.battery = 70
    st.rssi = -60
    return st


def test_jsonl_roast_content_and_rotation(tmp_path):
    rec = Recorder(tmp_path, interval=999, log_format="jsonl")
    st = _state()

    rec.start_roast()
    rec.snapshot(st)
    rec.snapshot(st)
    rec.end_roast()

    files = sorted(tmp_path.glob("roast_*.jsonl"))
    assert len(files) == 1
    rows = [json.loads(line) for line in files[0].read_text().splitlines()]
    assert len(rows) == 4  # CHARGE + 2 snapshots + DROP

    assert rows[0]["event"] == CHARGE
    assert rows[0]["BT"] is None
    assert rows[1]["BT"] == 198.0
    assert rows[1]["ET"] == 185.5
    assert rows[1]["battery"] == 70
    assert rows[1]["rssi"] == -60
    assert rows[1]["event"] == ""
    assert rows[2]["BT"] == 198.0
    assert rows[-1]["event"] == DROP

    # A second roast must create a second file.
    rec.start_roast()
    rec.end_roast()
    assert len(list(tmp_path.glob("roast_*.jsonl"))) == 2


def test_csv_roast_has_header_and_rows(tmp_path):
    rec = Recorder(tmp_path, interval=999, log_format="csv")
    st = _state()

    rec.start_roast()
    rec.snapshot(st)
    rec.end_roast()

    files = list(tmp_path.glob("roast_*.csv"))
    assert len(files) == 1
    with files[0].open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert rows[0]["event"] == CHARGE
    assert rows[1]["BT"] == "198.0"
    assert rows[1]["ET"] == "185.5"
    assert rows[-1]["event"] == DROP


def test_channels_format_records_all_six(tmp_path):
    rec = Recorder(tmp_path, interval=999, log_format="channels")
    st = _state()
    st.channels[1] = 25.0

    rec.start_roast()
    rec.snapshot(st)
    rec.end_roast()

    files = list(tmp_path.glob("roast_*.csv"))
    rows = []
    with files[0].open(newline="") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
    data_row = rows[1]
    assert data_row["ch1"] == "25.0"
    assert data_row["ch4"] == "198.0"
    assert data_row["ch6"] == "185.5"
    assert data_row["ch2"] == ""
    assert data_row["event"] == ""


def test_snapshot_is_noop_without_active_roast(tmp_path):
    rec = Recorder(tmp_path, interval=999, log_format="jsonl")
    rec.snapshot(_state())
    assert list(tmp_path.glob("roast_*.jsonl")) == []


@pytest.mark.asyncio
async def test_sim_drives_state_and_recorder_end_to_end(tmp_path):
    """Sim -> state -> recorder produces a valid ramped roast log."""
    state = DeviceState(bt_channel=4, et_channel=6)
    rec = Recorder(tmp_path, interval=0.005, log_format="jsonl")
    sim = SimSource(state, interval=0.005, max_steps=15, start_temp=100, end_temp=200)

    await sim.start()
    rec.start_roast()
    rec_task = asyncio.create_task(rec.run(state))
    await sim.wait_done()
    await asyncio.sleep(0.05)  # let the recorder capture the final plateau
    rec.end_roast()
    rec_task.cancel()

    files = list(tmp_path.glob("roast_*.jsonl"))
    assert len(files) == 1
    rows = [json.loads(line) for line in files[0].read_text().splitlines()]
    assert rows[0]["event"] == CHARGE
    assert rows[-1]["event"] == DROP

    bt_vals = [r["BT"] for r in rows if r["BT"] is not None]
    et_vals = [r["ET"] for r in rows if r["ET"] is not None]
    assert len(bt_vals) >= 2
    assert bt_vals[-1] > bt_vals[0]  # ramp went up
    assert et_vals[0] < bt_vals[0]  # ET trails BT
    assert rows[0]["t_epoch"] <= rows[-1]["t_epoch"]  # chronological
