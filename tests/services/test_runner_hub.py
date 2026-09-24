"""RunnerHub connect/disconnect and RPC timeout."""
import asyncio

import pytest

from app.services.runners.hub import RpcError, RpcTimeout, RunnerDisconnected, RunnerHub, reset_hub
from app.services.runners.protocol import encode_reply, encode_state, decode_message


class FakeWebSocket:
    def __init__(self):
        self.sent = []
        self.closed = None

    async def send_text(self, data: str):
        self.sent.append(data)

    async def close(self, code=1000):
        self.closed = code


@pytest.fixture
def hub(mock_redis, monkeypatch):
    monkeypatch.setattr("app.core.redis.redis_client", mock_redis)
    monkeypatch.setattr("app.services.runners.cache.redis_mod.redis_client", mock_redis)
    reset_hub()
    return RunnerHub()


@pytest.mark.asyncio
async def test_rpc_timeout(hub):
    ws = FakeWebSocket()
    await hub.attach(1, ws)
    with pytest.raises(RpcTimeout):
        await hub.rpc(1, "dhcp.status", timeout=0.05)
    assert hub.is_connected(1)


@pytest.mark.asyncio
async def test_rpc_success(hub):
    ws = FakeWebSocket()
    await hub.attach(7, ws)
    task = asyncio.create_task(hub.rpc(7, "dhcp.start", timeout=1.0))
    await asyncio.sleep(0.01)
    assert ws.sent
    msg = decode_message(ws.sent[0])
    await hub.handle_incoming(7, encode_reply(msg["id"], ok=True, result={"status": "running"}))
    assert await task == {"status": "running"}


@pytest.mark.asyncio
async def test_rpc_error_and_disconnect(hub):
    ws = FakeWebSocket()
    await hub.attach(3, ws)
    task = asyncio.create_task(hub.rpc(3, "dhcp.start", timeout=1.0))
    await asyncio.sleep(0.01)
    msg = decode_message(ws.sent[0])
    await hub.handle_incoming(3, encode_reply(msg["id"], ok=False, error="boom"))
    with pytest.raises(RpcError, match="boom"):
        await task
    await hub.detach(3, ws)
    assert not hub.is_connected(3)
    with pytest.raises(RunnerDisconnected):
        await hub.rpc(3, "dhcp.status")


@pytest.mark.asyncio
async def test_detach_fails_in_flight_rpc(hub):
    ws = FakeWebSocket()
    await hub.attach(4, ws)
    task = asyncio.create_task(hub.rpc(4, "dhcp.status", timeout=2.0))
    await asyncio.sleep(0.01)
    await hub.detach(4, ws)
    with pytest.raises(RunnerDisconnected):
        await task


@pytest.mark.asyncio
async def test_state_message_cached(hub, mock_redis, monkeypatch):
    monkeypatch.setattr("app.services.runners.hub.SessionLocal", lambda: _NoopSession())
    ws = FakeWebSocket()
    await hub.attach(9, ws)
    await hub.handle_incoming(9, encode_state({"running": True, "isos": []}))
    from app.services.runners.cache import cached_state

    assert cached_state(9)["running"] is True


class _NoopSession:
    def close(self):
        return None

    def rollback(self):
        return None
