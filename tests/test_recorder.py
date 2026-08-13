"""Recorder tests: continuous JSONL/CSV logging and a sim-driven E2E run."""

import asyncio
import csv
import json

import pytest

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


def test_jsonl_log_content(tmp_path):
    rec = Recorder(tmp_path, interval=999, log_format="jsonl")
    st = _state()

    rec.start()
    rec.snapshot(st)
    rec.snapshot(st)
    rec.close()

    files = sorted(tmp_path.glob("roast_*.jsonl"))
    assert len(files) == 1
    rows = [json.loads(line) for line in files[0].read_text().splitlines()]
    assert len(rows) == 2

    assert rows[0]["BT"] == 198.0
    assert rows[0]["ET"] == 185.5
    assert rows[0]["battery"] == 70
    assert rows[0]["rssi"] == -60
    assert rows[1]["BT"] == 198.0
    assert "event" not in rows[0]


def test_csv_log_has_header_and_rows(tmp_path):
    rec = Recorder(tmp_path, interval=999, log_format="csv")
    st = _state()

    rec.start()
    rec.snapshot(st)
    rec.close()

    files = list(tmp_path.glob("roast_*.csv"))
    assert len(files) == 1
    with files[0].open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert rows[0]["BT"] == "198.0"
    assert rows[0]["ET"] == "185.5"
    assert "event" not in rows[0]


def test_channels_format_records_all_six(tmp_path):
    rec = Recorder(tmp_path, interval=999, log_format="channels")
    st = _state()
    st.channels[1] = 25.0

    rec.start()
    rec.snapshot(st)
    rec.close()

    files = list(tmp_path.glob("roast_*.csv"))
    with files[0].open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert rows[0]["ch1"] == "25.0"
    assert rows[0]["ch4"] == "198.0"
    assert rows[0]["ch6"] == "185.5"
    assert rows[0]["ch2"] == ""


def test_snapshot_is_noop_before_start(tmp_path):
    rec = Recorder(tmp_path, interval=999, log_format="jsonl")
    rec.snapshot(_state())
    assert list(tmp_path.glob("roast_*.jsonl")) == []


@pytest.mark.asyncio
async def test_sim_drives_state_and_recorder_end_to_end(tmp_path):
    """Sim -> state -> recorder produces a valid ramped log."""
    state = DeviceState(bt_channel=4, et_channel=6)
    rec = Recorder(tmp_path, interval=0.005, log_format="jsonl")
    sim = SimSource(state, interval=0.005, max_steps=15, start_temp=100, end_temp=200)

    await sim.start()
    rec.start()
    rec_task = asyncio.create_task(rec.run(state))
    await sim.wait_done()
    await asyncio.sleep(0.05)  # let the recorder capture the final plateau
    rec_task.cancel()
    rec.close()

    files = list(tmp_path.glob("roast_*.jsonl"))
    assert len(files) == 1
    rows = [json.loads(line) for line in files[0].read_text().splitlines()]
    assert len(rows) >= 2

    bt_vals = [r["BT"] for r in rows if r["BT"] is not None]
    et_vals = [r["ET"] for r in rows if r["ET"] is not None]
    assert len(bt_vals) >= 2
    assert bt_vals[-1] > bt_vals[0]  # ramp went up
    assert et_vals[0] < bt_vals[0]  # ET trails BT
    assert rows[0]["t_epoch"] <= rows[-1]["t_epoch"]  # chronological
