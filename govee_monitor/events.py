"""Event sources: keyboard thread, auto-charge threshold, optional control file.

Events flow through a single ``on_event(evt)`` coroutine handed to every
source. Two event names are used: ``CHARGE`` (charge/drop beans in) and
``DROP`` (drop the roast).

Keyboard input runs on a background thread because Windows' Proactor event
loop does not support ``loop.add_reader(sys.stdin)``. The thread hands
events back to the loop with ``asyncio.run_coroutine_threadsafe``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path

from .state import DeviceState

log = logging.getLogger(__name__)

CHARGE = "CHARGE"
DROP = "DROP"
QUIT = "QUIT"

_EVENT_NAMES = (CHARGE, DROP)


def parse_key(line: str) -> str | None:
    """Map a console line to an event name (c/d/q or full words)."""
    s = line.strip().lower()
    if s in ("c", "charge"):
        return CHARGE
    if s in ("d", "drop"):
        return DROP
    if s in ("q", "quit", "exit"):
        return QUIT
    return None


class KeyboardSource:
    """Background-thread console input: ``c``/``d`` trigger charge/drop."""

    def __init__(
        self,
        on_event,
        on_quit=None,
        prompt: str = "c=CHARGE d=DROP q=quit > ",
    ) -> None:
        self.on_event = on_event
        self.on_quit = on_quit
        self.prompt = prompt
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._thread = threading.Thread(
            target=self._reader, name="keyboard", daemon=True
        )
        self._thread.start()

    async def stop(self) -> None:
        # The thread blocks on input(); it is a daemon and dies with the process.
        pass

    def _reader(self) -> None:
        while True:
            try:
                line = input(self.prompt)
            except EOFError:
                return
            except (OSError, ValueError) as exc:
                log.warning("keyboard input unavailable: %s", exc)
                return
            evt = parse_key(line)
            if evt is None:
                continue
            if evt == QUIT and self.on_quit is not None:
                coro = self.on_quit()
            else:
                coro = self.on_event(evt)
            try:
                asyncio.run_coroutine_threadsafe(coro, self._loop)
            except RuntimeError:
                # Event loop already closed during shutdown; daemon thread ends here.
                return


class AutoChargeSource:
    """Send CHARGE the first time BT reaches ``threshold``.

    Re-arms after a DROP so each subsequent roast auto-charges again.
    """

    def __init__(
        self,
        state: DeviceState,
        threshold: float,
        on_event,
        *,
        poll: float = 1.0,
    ) -> None:
        self.state = state
        self.threshold = threshold
        self.on_event = on_event
        self.poll = poll
        self._fired = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()

    def rearm(self) -> None:
        self._fired = False

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.poll)
            bt = self.state.bt
            if not self._fired and bt is not None and bt >= self.threshold:
                self._fired = True
                log.info("auto-charge: BT %.1f >= threshold %.1f", bt, self.threshold)
                await self.on_event(CHARGE)


class FileSource:
    """Optional control file: writing CHARGE/DROP into it triggers events.

    Lets external tooling drive the roast without a console. The file is
    polled; its trimmed/uppercased content is matched against the event names.
    """

    def __init__(self, path: str | Path, on_event, *, poll: float = 0.5) -> None:
        self.path = path
        self.on_event = on_event
        self.poll = poll
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()

    async def _run(self) -> None:
        last: str | None = None
        while True:
            await asyncio.sleep(self.poll)
            try:
                content = Path(self.path).read_text(encoding="utf-8").strip().upper()
            except FileNotFoundError:
                continue
            except OSError as exc:
                log.warning("control file read error: %s", exc)
                continue
            if content == last:
                continue
            last = content
            if content in _EVENT_NAMES:
                log.info("control file event: %s", content)
                await self.on_event(content)
