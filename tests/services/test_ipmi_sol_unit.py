"""Unit tests for ipmitool SOL helpers (no live BMC)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.sol import ipmi_sol
from app.services.sol.base import SolUnavailable


def test_encode_sol_stdin_maps_cr_to_crlf():
    assert ipmi_sol.encode_sol_stdin(b"") == b""
    assert ipmi_sol.encode_sol_stdin(b"hi") == b"hi"
    assert ipmi_sol.encode_sol_stdin(b"a\rb") == b"a\r\nb"
    assert ipmi_sol.encode_sol_stdin(b"a\r\nb") == b"a\r\nb"


def test_build_ipmitool_args_includes_subcommand():
    args = ipmi_sol.build_ipmitool_args("10.0.0.1", "root", 623, "sol", "activate")
    assert args[:4] == ["ipmitool", "-I", "lanplus", "-H"]
    assert "10.0.0.1" in args
    assert "-E" in args
    assert args[-2:] == ["sol", "activate"]


@pytest.mark.asyncio
async def test_run_ipmitool_success():
    profile = ipmi_sol.IpmiSolProfile()
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(b"ok", b""))
    proc.returncode = 0
    with patch.object(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)):
        stdout, stderr, code = await profile._run_ipmitool(
            "10.0.0.2", "admin", "secret", 623, "sol", "info"
        )
    assert stdout == b"ok"
    assert stderr == b""
    assert code == 0


@pytest.mark.asyncio
async def test_run_ipmitool_timeout():
    profile = ipmi_sol.IpmiSolProfile()
    proc = MagicMock()
    proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
    with patch.object(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)):
        with patch.object(asyncio, "timeout", side_effect=lambda _s: _TimeoutCtx()):
            with pytest.raises(SolUnavailable, match="timed out"):
                await profile._run_ipmitool(
                    "10.0.0.2", "admin", "secret", 623, "sol", "info", max_wait=0.01
                )


class _TimeoutCtx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        raise TimeoutError()


def test_build_ipmitool_args_custom_port_and_escape():
    args = ipmi_sol.build_ipmitool_args(
        "bmc.example", "operator", 6233, "sol", "deactivate"
    )
    assert args[0] == "ipmitool"
    assert "-H" in args and "bmc.example" in args
    assert "-U" in args and "operator" in args
    assert "-p" in args and "6233" in args
    assert "-e" in args
    escape_idx = args.index("-e")
    assert len(args[escape_idx + 1]) == 1
    assert args[-2:] == ["sol", "deactivate"]


def test_build_ipmitool_args_never_includes_password():
    args = ipmi_sol.build_ipmitool_args("1.2.3.4", "root", 623, "sol", "info")
    assert "-E" in args
    assert "secret" not in args
    assert all(not arg.startswith("IPMI_") for arg in args)


def test_encode_sol_stdin_preserves_lf_only_lines():
    assert ipmi_sol.encode_sol_stdin(b"line1\nline2") == b"line1\nline2"
    assert ipmi_sol.encode_sol_stdin(b"\r\r") == b"\r\n\r\n"


@pytest.mark.asyncio
async def test_tune_interactive_sol_sets_low_accumulate():
    profile = ipmi_sol.IpmiSolProfile()
    calls: list[tuple] = []

    async def _fake_run(*args, **_kwargs):
        calls.append(args)
        return b"", b"", 0

    profile._run_ipmitool = _fake_run  # type: ignore[method-assign]
    await profile._tune_interactive_sol("10.0.0.5", "admin", "pass", 623)
    subcommands = [args[4:] for args in calls]
    assert ("sol", "set", "character-accumulate-level", "1") in subcommands
    assert ("sol", "set", "character-send-threshold", "1") in subcommands


@pytest.mark.asyncio
async def test_open_session_starts_ipmitool_activate(monkeypatch):
    profile = ipmi_sol.IpmiSolProfile()
    server = MagicMock()
    server.ipmi_hostname = "10.0.0.5"
    server.ipmi_username = "admin"
    server.ipmi_password = "pass"
    server.ipmi_port = 623

    monkeypatch.setattr(ipmi_sol.shutil, "which", lambda _name: "/usr/bin/ipmitool")
    monkeypatch.setattr(
        profile,
        "credentials",
        lambda _srv: ("10.0.0.5", "admin", "pass", 623),
    )

    deactivate_calls = 0

    async def _fake_run(*_args, **_kwargs):
        nonlocal deactivate_calls
        deactivate_calls += 1
        return b"", b"", 0

    monkeypatch.setattr(profile, "_run_ipmitool", _fake_run)
    monkeypatch.setattr(ipmi_sol.pty, "openpty", lambda: (11, 12))
    monkeypatch.setattr(ipmi_sol, "_pty_set_raw", lambda _fd: None)
    monkeypatch.setattr(ipmi_sol.os, "close", lambda _fd: None)

    proc = MagicMock()
    proc.returncode = None
    proc.terminate = MagicMock()
    proc.wait = AsyncMock()
    proc.kill = MagicMock()

    create_proc = AsyncMock(return_value=proc)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_proc)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    session = await profile.open_session(server)
    assert isinstance(session, ipmi_sol.IpmiSolByteSession)
    assert deactivate_calls >= 3
    create_proc.assert_called_once()
    call_args = create_proc.call_args[0]
    assert call_args[-2:] == ("sol", "activate")
    await session.close()


@pytest.mark.asyncio
async def test_open_session_fails_when_ipmitool_missing(monkeypatch):
    profile = ipmi_sol.IpmiSolProfile()
    monkeypatch.setattr(ipmi_sol.shutil, "which", lambda _name: None)
    with pytest.raises(SolUnavailable, match="not installed"):
        await profile.open_session(MagicMock())


@pytest.mark.asyncio
async def test_open_session_raises_when_activate_exits_immediately(monkeypatch):
    profile = ipmi_sol.IpmiSolProfile()
    monkeypatch.setattr(ipmi_sol.shutil, "which", lambda _name: "/usr/bin/ipmitool")
    monkeypatch.setattr(
        profile,
        "credentials",
        lambda _srv: ("10.0.0.5", "admin", "pass", 623),
    )
    monkeypatch.setattr(profile, "_run_ipmitool", AsyncMock(return_value=(b"", b"", 0)))
    monkeypatch.setattr(ipmi_sol.pty, "openpty", lambda: (21, 22))
    monkeypatch.setattr(ipmi_sol, "_pty_set_raw", lambda _fd: None)
    monkeypatch.setattr(ipmi_sol.os, "close", lambda _fd: None)

    proc = MagicMock()
    proc.returncode = 1
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
    )
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    with pytest.raises(SolUnavailable, match="exited immediately"):
        await profile.open_session(MagicMock())
