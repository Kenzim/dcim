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
