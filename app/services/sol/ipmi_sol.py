"""IPMI 2.0 Serial-over-LAN via ipmitool (PTY)."""
from __future__ import annotations

import asyncio
import logging
import os
import pty
import shutil
import termios
import tty
from typing import List, Optional

from app.models.server import Server
from app.services.sol.base import SolByteSession, SolProfile, SolUnavailable

logger = logging.getLogger(__name__)

# Rare control char so typed ``~.`` in a serial session does not detach ipmitool.
_SOL_ESCAPE = "\x1d"


def _pty_set_raw(fd: int) -> None:
    """Disable canonical input so each keystroke reaches ipmitool immediately."""
    tty.setraw(fd, when=termios.TCSANOW)


def encode_sol_stdin(data: bytes) -> bytes:
    """Map xterm Enter (CR) to CRLF.

    ipmitool only honors the local SOL escape immediately after a newline.
    xterm.js sends ``\\r`` for Enter; without an LF the Ctrl-] / ``?``
    sequences never fire.
    """
    if not data:
        return data
    return data.replace(b"\r\n", b"\r").replace(b"\r", b"\r\n")


def build_ipmitool_args(
    hostname: str,
    username: str,
    port: int,
    *subcommand: str,
) -> List[str]:
    """ipmitool argv. Password is never on the command line (``-E`` / IPMI_PASSWORD)."""
    return [
        "ipmitool",
        "-I",
        "lanplus",
        "-H",
        hostname,
        "-U",
        username,
        "-E",
        "-L",
        "ADMINISTRATOR",
        "-p",
        str(port),
        "-e",
        _SOL_ESCAPE,
        *subcommand,
    ]


class IpmiSolByteSession(SolByteSession):
    def __init__(self, master_fd: int, proc: asyncio.subprocess.Process, deactivate):
        self._master_fd = master_fd
        self._proc = proc
        self._deactivate = deactivate
        self._closed = False

    async def write(self, data: bytes) -> None:
        if self._closed or not data:
            return
        view = memoryview(encode_sol_stdin(data))
        while view:
            n = await asyncio.to_thread(os.write, self._master_fd, view)
            if n <= 0:
                break
            view = view[n:]

    async def read(self, max_bytes: int = 4096) -> bytes:
        if self._closed:
            return b""
        try:
            return await asyncio.to_thread(os.read, self._master_fd, max_bytes)
        except OSError:
            return b""

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            os.close(self._master_fd)
        except OSError:
            pass
        if self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), 5)
            except (asyncio.TimeoutError, ProcessLookupError):
                self._proc.kill()
                try:
                    await asyncio.wait_for(self._proc.wait(), 3)
                except (asyncio.TimeoutError, ProcessLookupError):
                    pass
        try:
            await self._deactivate()
        except Exception:  # noqa: BLE001
            logger.debug("ipmitool sol deactivate after close failed", exc_info=True)


class IpmiSolProfile(SolProfile):
    id = "ipmi_sol"
    display_name = "IPMI Serial-over-LAN (ipmitool)"

    def _require_ipmitool(self) -> None:
        if not shutil.which("ipmitool"):
            raise SolUnavailable("ipmitool is not installed on the Rackflow host")

    def probe(self, server: Server) -> None:
        self._require_ipmitool()
        self.credentials(server)

    async def _run_ipmitool(
        self,
        hostname: str,
        username: str,
        password: str,
        port: int,
        *subcommand: str,
        timeout: float = 12,
    ) -> tuple[bytes, bytes, int]:
        args = build_ipmitool_args(hostname, username, port, *subcommand)
        env = {**os.environ, "IPMI_PASSWORD": password}
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise SolUnavailable(f"ipmitool {' '.join(subcommand)} timed out") from exc
        return stdout or b"", stderr or b"", proc.returncode or 0

    async def open_session(self, server: Server) -> SolByteSession:
        self._require_ipmitool()
        hostname, username, password, port = self.credentials(server)
        env = {**os.environ, "IPMI_PASSWORD": password}

        # Clear a stale exclusive SOL payload from a previous crash/idle timeout.
        try:
            await self._run_ipmitool(hostname, username, password, port, "sol", "deactivate")
        except SolUnavailable:
            pass

        master_fd, slave_fd = pty.openpty()
        try:
            _pty_set_raw(slave_fd)
            try:
                _pty_set_raw(master_fd)
            except termios.error:
                pass
        except termios.error as exc:
            os.close(master_fd)
            os.close(slave_fd)
            raise SolUnavailable(f"Failed to set SOL PTY to raw mode: {exc}") from exc

        proc: Optional[asyncio.subprocess.Process] = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *build_ipmitool_args(hostname, username, port, "sol", "activate"),
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                env=env,
                start_new_session=True,
            )
        except Exception as exc:  # noqa: BLE001
            os.close(master_fd)
            os.close(slave_fd)
            raise SolUnavailable(f"Failed to start IPMI SOL: {exc}") from exc
        os.close(slave_fd)

        await asyncio.sleep(0.4)
        if proc.returncode is not None:
            try:
                os.close(master_fd)
            except OSError:
                pass
            raise SolUnavailable("ipmitool sol activate exited immediately")

        async def deactivate() -> None:
            try:
                await self._run_ipmitool(
                    hostname, username, password, port, "sol", "deactivate", timeout=8
                )
            except SolUnavailable:
                pass

        return IpmiSolByteSession(master_fd, proc, deactivate)
