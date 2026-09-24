"""Async TCP stream with the same recv/send/close surface as a WebSocket."""
from __future__ import annotations

import asyncio
from typing import Optional


class TcpByteStream:
    """Byte stream over asyncio streams. ``recv`` returns ``None`` on EOF."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._reader = reader
        self._writer = writer

    async def recv(self) -> Optional[bytes]:
        data = await self._reader.read(65536)
        return data or None

    async def send(self, data: bytes) -> None:
        if not data:
            return
        self._writer.write(data)
        await self._writer.drain()

    async def close(self) -> None:
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except (ConnectionError, OSError):
            pass


class TcpConnect:
    """Async context manager that opens a TCP connection and yields ``TcpByteStream``."""

    def __init__(self, host: str, port: int, *, timeout: float = 20.0):
        self._host = host
        self._port = int(port)
        self._timeout = timeout
        self._stream: Optional[TcpByteStream] = None

    async def __aenter__(self) -> TcpByteStream:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self._host, self._port),
            self._timeout,
        )
        self._stream = TcpByteStream(reader, writer)
        return self._stream

    async def __aexit__(self, *exc) -> None:
        del exc
        if self._stream is not None:
            await self._stream.close()
            self._stream = None
