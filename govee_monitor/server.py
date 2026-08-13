"""Artisan websockets bridge.

Artisan acts as the WebSocket *client*; this module is the server. The
protocol is request/response: a request carries ``command`` and ``id``, and
the response must echo the ``id``.

Artisan's Web Sockets tab defaults to a single ``getData`` request, so one
round-trip returns both BT and ET. Unknown commands (e.g.
``getCurrentRoastingStep``) and ``keepAlive`` are answered with an echoed id
and an empty ``data`` so Artisan never hangs waiting for a reply.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import websockets

from .state import DeviceState

log = logging.getLogger(__name__)


class DataServer:
    def __init__(
        self,
        state: DeviceState,
        node_bt: str = "BT",
        node_et: str = "ET",
    ) -> None:
        self.state = state
        self.node_bt = node_bt
        self.node_et = node_et
        self._ws_server: Any = None

    @property
    def port(self) -> int:
        if self._ws_server is not None and self._ws_server.sockets:
            return self._ws_server.sockets[0].getsockname()[1]
        return 0

    async def start(self, host: str = "127.0.0.1", port: int = 9090) -> None:
        self._ws_server = await websockets.serve(self._handler, host, port)
        log.info("websockets server listening on ws://%s:%s", host, port)

    async def stop(self) -> None:
        if self._ws_server is not None:
            self._ws_server.close()
            await self._ws_server.wait_closed()
            self._ws_server = None

    async def _handler(self, ws: Any) -> None:
        try:
            async for raw in ws:
                await self._on_message(ws, raw)
        except websockets.ConnectionClosed:
            pass

    async def _on_message(self, ws: Any, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            log.warning("non-JSON message from client: %r", raw)
            return
        if not isinstance(msg, dict):
            log.warning("non-object message from client: %r", msg)
            return

        command = msg.get("command")
        rid = msg.get("id")
        if command == "getData":
            data: dict[str, float] = {}
            bt = self.state.bt
            et = self.state.et
            if bt is not None:
                data[self.node_bt] = bt
            if et is not None:
                data[self.node_et] = et
            response = {"id": rid, "data": data}
        else:
            response = {"id": rid, "data": {}}
        await ws.send(json.dumps(response))
