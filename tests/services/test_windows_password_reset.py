"""Windows Administrator password reset over SOL with a fake serial getty."""
import asyncio
import logging
from types import SimpleNamespace

import pytest

from app.models.service import ProvisioningSource, ServiceStatus
from app.models.server_activity import ServerActivity, ServerActivityStatus
from app.services.sol import register_profile, unregister_profile
from app.services.sol import windows_password as wp
from app.services.sol.base import SolByteSession, SolProfile
from app.services.sol.hub import _hubs, shutdown_sol_hubs

PROMPT = b"C:\\Windows\\system32\r\nRF> "
OK = b"The command completed successfully.\r\n\r\n"
POLICY_ERROR = (
    b"The password does not meet the password policy requirements. Check the minimum "
    b"password length, password complexity and password history requirements.\r\n\r\n"
    b"More help is available by typing NET HELPMSG 2245.\r\n\r\n"
)


class _GettySession(SolByteSession):
    """Mimics serial-admin-cmd.ps1: no echo, Ctrl-U clears, CR runs the line."""

    def __init__(self, mode: str = "ok"):
        self.mode = mode
        self.lines: list[str] = []
        self._buf = bytearray()
        self._q: asyncio.Queue[bytes] = asyncio.Queue()

    async def write(self, data: bytes) -> None:
        for b in data:
            if b == 0x15:
                self._buf.clear()
            elif b == 0x0D:
                self._run(self._buf.decode("ascii").strip())
                self._buf.clear()
            else:
                self._buf.append(b)

    def _run(self, line: str) -> None:
        if self.mode == "silent":
            return
        if self.mode == "other_program":
            # Something else owns the port (SAC, a login banner, a shell): it
            # answers, but never with serial-admin-cmd.ps1's "<cwd>\r\nRF> ".
            self.lines.append(line)
            self._q.put_nowait(b"\r\nSAC>RF> \r\nlogin: ")
            return
        if not line:
            self._q.put_nowait(PROMPT)
            return
        self.lines.append(line)
        if line.startswith("net user"):
            if self.mode == "error":
                self._q.put_nowait(POLICY_ERROR + PROMPT)
            elif self.mode == "echo_error":
                # Hostile output that repeats the command line verbatim.
                self._q.put_nowait(line.encode() + b"\r\nSystem error 5 has occurred.\r\n" + PROMPT)
            else:
                self._q.put_nowait(OK + PROMPT)
        else:
            self._q.put_nowait(b"        1 file(s) moved.\r\n" + PROMPT)

    async def read(self, max_bytes: int = 4096) -> bytes:
        return (await self._q.get())[:max_bytes]

    async def close(self) -> None:
        self._q.put_nowait(b"")


class _GettyProfile(SolProfile):
    id = "fake_getty"
    display_name = "Fake getty"

    def __init__(self, mode: str):
        self.session = _GettySession(mode)

    def probe(self, server) -> None:
        return None

    async def open_session(self, server) -> SolByteSession:
        return self.session


@pytest.fixture
def setup(db_session, mock_redis, monkeypatch):
    monkeypatch.setattr("app.core.redis.redis_client", mock_redis)
    monkeypatch.setattr(wp, "PROMPT_ATTEMPT_SECONDS", 0.3)
    monkeypatch.setattr(wp, "COMMAND_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(wp, "LOG_SCRUB_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(wp, "POST_PROMPT_DELAY_SECONDS", 0.0)
    templates = {
        "windows-server-2022": SimpleNamespace(os_type="windows"),
        "ubuntu-cloud-image": SimpleNamespace(os_type="linux"),
    }
    monkeypatch.setattr(
        wp, "get_template_service", lambda: SimpleNamespace(get_template=templates.get)
    )

    from app.dao.location_dao import LocationDAO
    from app.dao.server_dao import ServerDAO
    from app.dao.service_dao import ServiceDAO

    profiles = []

    def make(mode="ok", template_id="windows-server-2022"):
        profile = _GettyProfile(mode)
        register_profile(profile)
        profiles.append(profile)
        location = LocationDAO.create(db_session, name=f"loc-wpr-{len(profiles)}")
        server = ServerDAO.create(
            db_session,
            name=f"wpr-{len(profiles)}",
            server_ip=f"10.9.8.{len(profiles)}",
            plugin_name="ipmi",
            plugin_config={"hostname": "10.9.8.1", "username": "admin", "password": "x"},
            location_id=location.id,
            sol_profile="fake_getty",
        )
        service = ServiceDAO.create_bare_metal(
            db_session,
            name="wpr",
            server_id=server.id,
            provisioning_source=ProvisioningSource.INTERNAL,
            status=ServiceStatus.ACTIVE,
        )
        service.config = {
            "template_id": template_id,
            "template_parameters": {"admin_password": "OldPass1!"},
        }
        ServiceDAO.update(db_session, service)
        return service, server, profile

    yield make
    unregister_profile("fake_getty")


def _activities(db_session, service_id):
    return db_session.query(ServerActivity).filter(ServerActivity.service_id == service_id).all()


@pytest.mark.asyncio
async def test_prompt_detected_sets_password(setup, db_session, caplog):
    caplog.set_level(logging.DEBUG)
    service, server, profile = setup("ok")
    password = "N3w&Pa|ss^<x>"
    try:
        result = await wp.reset_windows_admin_password(
            db_session, service, server, password, source="test"
        )
    finally:
        hub = _hubs.get(server.id)
        capture = bytes(hub.capture) if hub else b""
        await shutdown_sol_hubs()

    assert result == {"status": "ok", "username": "Administrator", "generated": False}
    assert profile.session.lines[0] == "net user Administrator N3w^&Pa^|ss^^^<x^> /Y"
    assert profile.session.lines[1] == wp.LOG_SCRUB_COMMAND
    db_session.refresh(service)
    assert service.config["template_parameters"]["admin_password"] == password
    acts = _activities(db_session, service.id)
    assert [a.status for a in acts] == [ServerActivityStatus.SUCCESS]
    blob = repr([(a.message, a.details) for a in acts]) + caplog.text + capture.decode()
    assert password not in blob
    assert wp.escape_for_cmd(password) not in blob


@pytest.mark.asyncio
async def test_password_overlapping_success_text_still_detected(setup, db_session):
    # Viewer masking must not hide the success line from the matcher.
    service, server, _profile = setup("ok")
    try:
        result = await wp.reset_windows_admin_password(
            db_session, service, server, "successfully", source="test"
        )
        capture = bytes(_hubs[server.id].capture)
    finally:
        await shutdown_sol_hubs()
    assert result["status"] == "ok"
    assert b"successfully" not in capture


@pytest.mark.asyncio
async def test_generated_password_is_returned_and_cmd_safe(setup, db_session):
    service, server, profile = setup("ok")
    try:
        result = await wp.reset_windows_admin_password(db_session, service, server, None, source="test")
    finally:
        await shutdown_sol_hubs()
    assert result["generated"] is True
    password = result["password"]
    assert wp.escape_for_cmd(password) == password
    wp.validate_windows_password(password)
    assert profile.session.lines[0] == f"net user Administrator {password} /Y"


@pytest.mark.asyncio
async def test_no_prompt_is_clear_error(setup, db_session, caplog):
    caplog.set_level(logging.DEBUG)
    service, server, profile = setup("silent")
    try:
        with pytest.raises(wp.WindowsPasswordResetError) as exc:
            await wp.reset_windows_admin_password(
                db_session, service, server, "Secret123!", source="test"
            )
    finally:
        await shutdown_sol_hubs()
    assert exc.value.status_code == 409
    assert "Serial admin console not available" in exc.value.detail
    assert profile.session.lines == []
    acts = _activities(db_session, service.id)
    assert [a.status for a in acts] == [ServerActivityStatus.FAILED]
    assert "Secret123!" not in repr(acts[0].details) + caplog.text
    db_session.refresh(service)
    assert service.config["template_parameters"]["admin_password"] == "OldPass1!"


@pytest.mark.asyncio
async def test_password_never_typed_without_admin_prompt(setup, db_session):
    service, server, profile = setup("other_program")
    try:
        with pytest.raises(wp.WindowsPasswordResetError) as exc:
            await wp.reset_windows_admin_password(
                db_session, service, server, "Secret123!", source="test"
            )
    finally:
        await shutdown_sol_hubs()
    assert exc.value.status_code == 409
    assert "Serial admin console not available" in exc.value.detail
    # Only the wake-up newlines were sent; no command line, no password.
    assert profile.session.lines and set(profile.session.lines) == {""}


@pytest.mark.asyncio
async def test_net_user_error_is_failure(setup, db_session):
    service, server, profile = setup("error")
    try:
        with pytest.raises(wp.WindowsPasswordResetError) as exc:
            await wp.reset_windows_admin_password(db_session, service, server, "weak", source="test")
    finally:
        await shutdown_sol_hubs()
    assert exc.value.status_code == 422
    # The getty log is scrubbed on failure too.
    assert profile.session.lines[1] == wp.LOG_SCRUB_COMMAND
    assert "password policy requirements" in exc.value.detail
    assert "HELPMSG" not in exc.value.detail
    db_session.refresh(service)
    assert service.config["template_parameters"]["admin_password"] == "OldPass1!"


@pytest.mark.asyncio
async def test_echoed_password_is_redacted(setup, db_session, caplog):
    caplog.set_level(logging.DEBUG)
    service, server, _profile = setup("echo_error")
    password = "Echo&Me123"
    try:
        with pytest.raises(wp.WindowsPasswordResetError) as exc:
            await wp.reset_windows_admin_password(db_session, service, server, password, source="test")
        capture = bytes(_hubs[server.id].capture)
    finally:
        await shutdown_sol_hubs()
    escaped = wp.escape_for_cmd(password)
    assert "System error 5" in exc.value.detail
    for text in (exc.value.detail, capture.decode(), caplog.text, repr(_activities(db_session, service.id)[0].details)):
        assert password not in text
        assert escaped not in text
    assert b"********" in capture


@pytest.mark.asyncio
async def test_non_windows_rejected_without_touching_console(setup, db_session):
    service, server, profile = setup("ok", template_id="ubuntu-cloud-image")
    with pytest.raises(wp.WindowsPasswordResetError) as exc:
        await wp.reset_windows_admin_password(db_session, service, server, "Abc123!x", source="test")
    assert exc.value.status_code == 409
    assert server.id not in _hubs
    assert profile.session.lines == []


@pytest.mark.asyncio
async def test_hub_refuses_other_writers_while_exclusive(setup, db_session):
    from app.services.sol.base import SolUnavailable
    from app.services.sol.hub import ensure_hub

    _service, server, _profile = setup("ok")
    hub = await ensure_hub(server)
    try:
        async with hub.exclusive([b"secret"]) as owner:
            with pytest.raises(SolUnavailable):
                await hub.inject(b"x")
            with pytest.raises(SolUnavailable):
                async with hub.exclusive([]):
                    pass
            await hub.inject(b"\r", owner=owner)
        await hub.inject(b"\r")
    finally:
        await shutdown_sol_hubs()


@pytest.mark.parametrize(
    "password",
    ["", "has space", 'quo"te', "per%cent", "/flag", "*", "caf\u00e9", "x" * 128],
)
def test_validate_rejects_unsafe_passwords(password):
    with pytest.raises(wp.WindowsPasswordResetError):
        wp.validate_windows_password(password)


def test_generated_passwords_meet_complexity():
    for _ in range(200):
        pw = wp.generate_windows_password()
        assert len(pw) == 16
        assert any(c.islower() for c in pw) and any(c.isupper() for c in pw)
        assert any(c.isdigit() for c in pw)
        assert set(pw) <= set(
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" + wp.GENERATED_SYMBOLS
        )
        assert not pw.startswith(("-", "/"))
        wp.validate_windows_password(pw)
