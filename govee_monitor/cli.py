"""argparse + asyncio entry point that binds every task together.

Single process, single asyncio event loop, three concurrent pieces:
    BLE scanner / simulator  ->  DeviceState
                                   |-> websockets server (Artisan)
                                   |-> recorder (local JSONL/CSV)
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from . import __version__
from .recorder import Recorder
from .scanner import H5055Scanner, SimSource, list_h5055
from .server import DataServer
from .state import DeviceState

log = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="govee-monitor",
        description="Govee H5055 BLE -> Artisan coffee-roasting temperature bridge.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    p.add_argument(
        "--mac",
        default=None,
        help="filter by exact BLE MAC (default: prefix a4:c1:38)",
    )
    p.add_argument(
        "--bt-channel",
        type=int,
        default=4,
        help="sensor number (1-6) used as bean temperature BT (default 4)",
    )
    p.add_argument(
        "--et-channel",
        type=int,
        default=6,
        help="sensor number (1-6) used as exhaust temperature ET (default 6)",
    )

    p.add_argument("--host", default="127.0.0.1", help="websockets bind host (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=9090, help="websockets port (default 9090)")
    p.add_argument("--ws-path", default="/", help="websockets path (informational; all paths accepted)")
    p.add_argument("--node-bt", default="BT", help="Artisan node name for bean temp (default BT)")
    p.add_argument("--node-et", default="ET", help="Artisan node name for exhaust temp (default ET)")

    p.add_argument("--interval", type=float, default=2.0, help="recording interval in seconds (default 2)")
    p.add_argument("--out", default="./logs", help="output directory for log files (default ./logs)")
    p.add_argument(
        "--log-format",
        choices=["jsonl", "csv", "channels"],
        default="jsonl",
        help="log format: wide jsonl/csv or per-channel csv (default jsonl)",
    )
    p.add_argument(
        "--temp-divisor",
        type=float,
        default=1.0,
        help="divide raw readings by this (use 10 if firmware reports 0.1 deg)",
    )

    p.add_argument("--scan-mode", choices=["active", "passive"], default="active")
    p.add_argument(
        "--watchdog",
        type=float,
        default=30.0,
        help="seconds without advertisements before warning (default 30, 0=off)",
    )
    p.add_argument(
        "--restart-on-watchdog",
        action="store_true",
        help="restart the scan after 3 missed watchdog intervals",
    )

    p.add_argument("--sim", action="store_true", help="synthetic payloads, no BLE needed")
    p.add_argument("--list", action="store_true", help="scan ~5s and list discovered H5055 devices")

    p.add_argument("-v", "--verbose", action="count", default=0)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    try:
        if args.list:
            asyncio.run(_list_devices(args))
            return 0
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        log.info("interrupted")
    return 0


async def _list_devices(args: argparse.Namespace) -> None:
    print("Scanning for Govee H5055 (prefix a4:c1:38) for 5s ...")
    try:
        entries = await list_h5055(mac=args.mac, duration=5.0)
    except Exception as exc:
        print(f"BLE scan failed: {exc}")
        print("Make sure Bluetooth is enabled and the adapter supports LE.")
        return
    if not entries:
        print("No H5055 advertisements found. Is Bluetooth on and the device powered?")
        return
    for e in entries:
        note = f"  ({e['note']})" if e["note"] else ""
        print(
            f"{e['mac']}  rssi={e['rssi']}  battery={e['battery']}"
            f"  mask={e['mask']}  channels={e['channels']}  name={e['name']!r}{note}"
        )
    print("Tip: plug bean/exhaust probes and check the mask bits correspond to sensors 1-6.")


async def _run(args: argparse.Namespace) -> None:
    state = DeviceState(
        bt_channel=args.bt_channel,
        et_channel=args.et_channel,
        temp_divisor=args.temp_divisor,
    )
    server = DataServer(state, node_bt=args.node_bt, node_et=args.node_et)
    recorder = Recorder(args.out, args.interval, args.log_format)

    await server.start(args.host, args.port)

    if args.sim:
        data_source = SimSource(state, interval=args.interval)
        log.info(
            "sim mode: BT=ch%s ET=ch%s, ramp 100->200 C (interval %.1fs)",
            args.bt_channel,
            args.et_channel,
            args.interval,
        )
    else:
        data_source = H5055Scanner(
            state,
            mac=args.mac,
            scan_mode=args.scan_mode,
            watchdog=args.watchdog,
            restart_on_watchdog=args.restart_on_watchdog,
        )

    try:
        await data_source.start()
    except Exception as exc:
        log.error("failed to start BLE scan: %s", exc)
        log.error(
            "check that Bluetooth is enabled and the adapter supports LE; "
            "use --sim to test the pipeline without BLE"
        )
        await server.stop()
        return

    recorder.start()
    recorder_task = asyncio.create_task(recorder.run(state))
    try:
        await asyncio.Event().wait()
    finally:
        recorder_task.cancel()
        recorder.close()
        await data_source.stop()
        await server.stop()
