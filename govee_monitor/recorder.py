"""JSONL/CSV recording of temperature snapshots.

A single log file is opened when the recorder starts and written to
continuously until the process exits. Snapshots are written every
``interval`` seconds. On shutdown the current file is flushed and closed.
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .state import DeviceState

log = logging.getLogger(__name__)


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _safe_iso() -> str:
    """UTC timestamp safe for Windows filenames (no colons, unique)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S-%f")


class Recorder:
    def __init__(
        self,
        out_dir: str | Path = "./logs",
        interval: float = 2.0,
        log_format: str = "jsonl",
    ) -> None:
        self.out_dir = Path(out_dir)
        self.interval = interval
        self.log_format = log_format
        self._fh: object | None = None
        self._csv_writer: Any | None = None
        self._start_iso: str | None = None

    def start(self) -> None:
        """Open a new log file for this run."""
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._start_iso = _safe_iso()
        self._csv_writer = None
        ext = "jsonl" if self.log_format == "jsonl" else "csv"
        path = self.out_dir / f"roast_{self._start_iso}.{ext}"
        self._fh = path.open("w", encoding="utf-8", newline="")
        log.info("started log file %s", path)

    def snapshot(self, state: DeviceState) -> None:
        if self._fh is None:
            return
        self._write_row(self._row_dict(state))

    def _row_dict(self, state: DeviceState) -> dict:
        row = {
            "t_iso": _iso_now(),
            "t_epoch": round(time.time(), 3),
            "battery": state.battery,
            "rssi": state.rssi,
        }
        if self.log_format == "channels":
            for ch in range(1, 7):
                row[f"ch{ch}"] = state.channels.get(ch)
            return row
        row["BT"] = state.bt
        row["ET"] = state.et
        return row

    def _columns(self) -> list[str]:
        if self.log_format == "channels":
            return [
                "t_iso",
                "t_epoch",
                "ch1",
                "ch2",
                "ch3",
                "ch4",
                "ch5",
                "ch6",
                "battery",
                "rssi",
            ]
        return ["t_iso", "t_epoch", "BT", "ET", "battery", "rssi"]

    def _write_row(self, row: dict) -> None:
        if self.log_format == "jsonl":
            self._fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        else:
            if self._csv_writer is None:
                self._csv_writer = csv.DictWriter(self._fh, fieldnames=self._columns())
                self._csv_writer.writeheader()
            self._csv_writer.writerow(row)
        # Flush each row so a hard kill / crash doesn't lose buffered data.
        self._fh.flush()

    async def run(self, state: DeviceState) -> None:
        """Snapshot loop; runs until cancelled."""
        while True:
            await asyncio.sleep(self.interval)
            self.snapshot(state)

    def close(self) -> None:
        """Flush and close the current file (e.g. on shutdown)."""
        if self._fh is not None:
            self._fh.flush()
            self._fh.close()
            self._fh = None
            self._csv_writer = None
