"""WebSockets server tests with a real in-process client."""

import asyncio
import json

import pytest
import websockets

from govee_monitor.server import DataServer
from govee_monitor.state import DeviceState


async def _start_server(state, **kw) -> tuple[DataServer, str]:
    server = DataServer(state, **kw)
    await server.start("127.0.0.1", 0)  # ephemeral port
    uri = f"ws://127.0.0.1:{server.port}"
    return server, uri


@pytest.mark.asyncio
async def test_get_data_returns_bt_and_et():
    state = DeviceState(bt_channel=4, et_channel=6)
    state.channels[4] = 198.0
    state.channels[6] = 185.5
    server, uri = await _start_server(state)
    try:
        async with websockets.connect(uri) as ws:
            await ws.send(json.dumps({"command": "getData", "id": 44683, "machine": 0}))
            raw = await asyncio.wait_for(ws.recv(), timeout=5)
            assert json.loads(raw) == {"id": 44683, "data": {"BT": 198.0, "ET": 185.5}}
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_get_data_omits_missing_values():
    state = DeviceState(bt_channel=4, et_channel=6)
    state.channels[4] = 198.0  # ET never seen
    server, uri = await _start_server(state)
    try:
        async with websockets.connect(uri) as ws:
            await ws.send(json.dumps({"command": "getData", "id": 1, "machine": 0}))
            raw = await asyncio.wait_for(ws.recv(), timeout=5)
            assert json.loads(raw) == {"id": 1, "data": {"BT": 198.0}}
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_node_names_are_configurable():
    state = DeviceState(bt_channel=4, et_channel=6)
    state.channels[4] = 198.0
    state.channels[6] = 185.5
    server, uri = await _start_server(state, node_bt="Beans", node_et="Exhaust")
    try:
        async with websockets.connect(uri) as ws:
            await ws.send(json.dumps({"command": "getData", "id": 7, "machine": 0}))
            raw = await asyncio.wait_for(ws.recv(), timeout=5)
            assert json.loads(raw) == {"id": 7, "data": {"Beans": 198.0, "Exhaust": 185.5}}
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_unknown_command_echoes_id_with_empty_data():
    state = DeviceState()
    server, uri = await _start_server(state)
    try:
        async with websockets.connect(uri) as ws:
            await ws.send(json.dumps({"command": "getCurrentRoastingStep", "id": 42}))
            raw = await asyncio.wait_for(ws.recv(), timeout=5)
            assert json.loads(raw) == {"id": 42, "data": {}}
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_keepalive_echoes_id_with_empty_data():
    state = DeviceState()
    server, uri = await _start_server(state)
    try:
        async with websockets.connect(uri) as ws:
            await ws.send(json.dumps({"command": "keepAlive", "id": 99}))
            raw = await asyncio.wait_for(ws.recv(), timeout=5)
            assert json.loads(raw) == {"id": 99, "data": {}}
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_broadcast_charge_reaches_client():
    state = DeviceState()
    server, uri = await _start_server(state)
    try:
        async with websockets.connect(uri) as ws:
            await asyncio.sleep(0.05)  # let the server register the connection
            await server.broadcast({"Message": "CHARGE"})
            raw = await asyncio.wait_for(ws.recv(), timeout=5)
            assert json.loads(raw) == {"Message": "CHARGE"}
    finally:
        await server.stop()
