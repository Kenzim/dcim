"""SOL hub inject with a fake in-process profile (no BMC)."""
import asyncio

import pytest

from app.services.sol import register_profile, unregister_profile
from app.services.sol.base import SolByteSession, SolProfile
from app.services.sol.hub import inject_bytes, shutdown_sol_hubs


class _EchoSession(SolByteSession):
    def __init__(self):
        self._q: asyncio.Queue[bytes] = asyncio.Queue()
        self.writes: list[bytes] = []
        self.closed = False

    async def write(self, data: bytes) -> None:
        self.writes.append(data)
        await self._q.put(data)

    async def read(self, max_bytes: int = 4096) -> bytes:
        data = await self._q.get()
        return data[:max_bytes]

    async def close(self) -> None:
        self.closed = True
        await self._q.put(b"")


class _EchoProfile(SolProfile):
    id = "fake_sol"
    display_name = "Fake SOL"

    def __init__(self):
        self.session = _EchoSession()

    def probe(self, server) -> None:
        return None

    async def open_session(self, server) -> SolByteSession:
        return self.session


@pytest.mark.asyncio
async def test_hub_inject_echoes_with_wait(db_session, mock_redis, monkeypatch):
    monkeypatch.setattr("app.core.redis.redis_client", mock_redis)
    from app.dao.location_dao import LocationDAO
    from app.dao.server_dao import ServerDAO

    profile = _EchoProfile()
    register_profile(profile)
    try:
        location = LocationDAO.create(db_session, name="loc-sol-hub")
        server = ServerDAO.create(
            db_session,
            name="sol-hub-srv",
            server_ip="10.9.9.1",
            plugin_name="ipmi",
            plugin_config={"hostname": "10.9.9.1", "username": "admin", "password": "x"},
            location_id=location.id,
            sol_profile="fake_sol",
        )
        output = await inject_bytes(server, b"hello", wait_ms=400)
        assert b"hello" in output
        assert profile.session.writes == [b"hello"]
    finally:
        await shutdown_sol_hubs()
        unregister_profile("fake_sol")
