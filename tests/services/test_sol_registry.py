"""SOL registry, payload decode, and ipmitool argv (no password on argv)."""
from app.services.sol import (
    SolUnavailable,
    list_profiles,
    normalize_profile_id,
    sol_ready,
)
from app.services.sol.ipmi_sol import build_ipmitool_args
from app.services.sol.payload import MAX_SEND_BYTES, decode_sol_payload


def test_list_profiles_includes_ipmi_sol():
    ids = {p["id"] for p in list_profiles()}
    assert "ipmi_sol" in ids


def test_normalize_profile_id():
    assert normalize_profile_id(None) is None
    assert normalize_profile_id("") is None
    assert normalize_profile_id("none") is None
    assert normalize_profile_id("ipmi_sol") == "ipmi_sol"


def test_normalize_unknown_profile_raises():
    try:
        normalize_profile_id("redfish")
        assert False, "expected SolUnavailable"
    except SolUnavailable as exc:
        assert "Unknown" in exc.detail


def test_sol_ready_false_without_profile():
    class S:
        sol_profile = None

    assert sol_ready(S()) is False
    assert sol_ready(None) is False


def test_ipmi_sol_args_omit_password():
    args = build_ipmitool_args("10.1.2.3", "admin", 623, "sol", "activate")
    joined = " ".join(args)
    assert "secret" not in joined
    assert "-E" in args
    assert "-L" in args
    assert "ADMINISTRATOR" in args
    assert "-H" in args
    assert "sol" in args
    assert "activate" in args


def test_encode_sol_stdin_maps_cr_to_crlf():
    from app.services.sol.ipmi_sol import encode_sol_stdin

    assert encode_sol_stdin(b"") == b""
    assert encode_sol_stdin(b"a") == b"a"
    assert encode_sol_stdin(b"\r") == b"\r\n"
    assert encode_sol_stdin(b"\r\n") == b"\r\n"
    assert encode_sol_stdin(b"x\r?") == b"x\r\n?"


def test_pty_set_raw_disables_canonical_mode():
    import os
    import pty
    import termios

    from app.services.sol.ipmi_sol import _pty_set_raw

    master, slave = pty.openpty()
    try:
        _pty_set_raw(slave)
        attrs = termios.tcgetattr(slave)
        assert not (attrs[3] & termios.ICANON)
        assert not (attrs[3] & termios.ECHO)
    finally:
        os.close(master)
        os.close(slave)


def test_decode_utf8_and_size_cap():
    assert decode_sol_payload("root\n", "utf-8") == b"root\n"
    try:
        decode_sol_payload("x" * (MAX_SEND_BYTES + 1), "utf-8")
        assert False, "expected size error"
    except SolUnavailable as exc:
        assert "exceeds" in exc.detail


def test_decode_base64_and_empty():
    assert decode_sol_payload("YWI=", "base64") == b"ab"
    try:
        decode_sol_payload("", "utf-8")
        assert False, "expected empty error"
    except SolUnavailable:
        pass
