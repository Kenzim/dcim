"""Reset the Windows Administrator password of a bare-metal server over SOL.

Windows installs run ``serial-admin-cmd.ps1`` (see
``os_templates/windows-server-2022/firstboot.ps1``): a SYSTEM PowerShell loop
on COM2/COM1/COM3 that prints ``<cwd>\\r\\nRF> ``, reads one line (no echo;
Ctrl-U clears the line; CR ends it and the next non-EOL byte is swallowed),
runs it via ``cmd.exe /c <line>`` and writes stdout/stderr back followed by a
new prompt. Blank lines within two seconds of a prompt are ignored.

The password is never logged, stored in activity details, or returned in
error text, and is masked in the shared SOL capture shown to viewers. On
success it replaces ``service.config.template_parameters.admin_password``,
where the install already keeps the Administrator password.
"""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
import string
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.dao.installation_task_dao import InstallationTaskDAO
from app.dao.service_dao import ServiceDAO
from app.models.server import Server
from app.models.server_activity import ServerActivityEventType
from app.models.service import Service, ServiceType
from app.services.os_template_service import get_template_service
from app.services.server_activity_logger import (
    log_server_activity_failure,
    log_server_activity_success,
)
from app.services.sol.base import SolUnavailable
from app.services.sol.hub import SolHub, ensure_hub
from app.services.sol.registry import sol_ready

logger = logging.getLogger(__name__)

USERNAME = "Administrator"
ACTION = "windows_password_reset"

# Positive match on serial-admin-cmd.ps1's prompt: "<drive>:\<cwd>\r\nRF> ".
# Anything else (SAC, a login banner, another program) is not a prompt and the
# password is never typed.
PROMPT_RE = re.compile(rb"(?:^|\n)[A-Za-z]:\\[^\r\n]*\r\nRF> ")
SUCCESS_RE = re.compile(rb"command completed successfully", re.IGNORECASE)

# Symbols that survive PowerShell -> cmd.exe /c -> net.exe without escaping.
GENERATED_SYMBOLS = "-_.@#+"
# cmd.exe metacharacters made literal with a caret.
_CARET_ESCAPED = set("^&|<>()")
_MAX_LEN = 127

PROMPT_ATTEMPT_SECONDS = 3.0
COMMAND_TIMEOUT_SECONDS = 20.0
LOG_SCRUB_TIMEOUT_SECONDS = 6.0
# serial-admin-cmd.ps1 discards input for ~20 ms after writing a prompt.
POST_PROMPT_DELAY_SECONDS = 0.3

# Older installs log every executed line to this file; drop the reset line.
_GETTY_LOG = r"C:\Windows\Temp\serial-admin-cmd.log"
_GETTY_LOG_TMP = r"C:\Windows\Temp\rf-scrub.tmp"
LOG_SCRUB_COMMAND = (
    f"findstr /V /I /R /C:exec.net.user {_GETTY_LOG} > {_GETTY_LOG_TMP}"
    f" & move /Y {_GETTY_LOG_TMP} {_GETTY_LOG}"
)


class WindowsPasswordResetError(Exception):
    """Reset failed. ``detail`` is safe to show (never contains the password)."""

    def __init__(self, detail: str, status_code: int = 502):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def generate_windows_password(length: int = 16) -> str:
    """Random password that meets Windows complexity and needs no cmd escaping."""
    lower = string.ascii_lowercase
    upper = string.ascii_uppercase
    digits = string.digits
    alphabet = lower + upper + digits + GENERATED_SYMBOLS
    chars = [
        secrets.choice(lower),
        secrets.choice(upper),
        secrets.choice(digits),
        secrets.choice(GENERATED_SYMBOLS),
    ]
    chars += [secrets.choice(alphabet) for _ in range(max(length, 8) - len(chars))]
    secrets.SystemRandom().shuffle(chars)
    password = "".join(chars)
    if password[0] in "-/":
        password = secrets.choice(upper) + password[1:]
    return password


def validate_windows_password(password: str) -> None:
    """Reject passwords the serial getty or ``net user`` cannot take verbatim."""
    if not password:
        raise WindowsPasswordResetError("Password is empty", 400)
    if len(password) > _MAX_LEN:
        raise WindowsPasswordResetError(f"Password is longer than {_MAX_LEN} characters", 400)
    if any(ord(c) < 33 or ord(c) > 126 for c in password):
        raise WindowsPasswordResetError(
            "Password may only contain printable ASCII characters without spaces", 400
        )
    if '"' in password or "%" in password:
        raise WindowsPasswordResetError('Password may not contain " or %', 400)
    if password.startswith("/") or password == "*":
        raise WindowsPasswordResetError("Password may not start with / or be *", 400)


def escape_for_cmd(password: str) -> str:
    """Caret-escape cmd.exe metacharacters (``^ & | < > ( )``)."""
    return "".join(f"^{c}" if c in _CARET_ESCAPED else c for c in password)


def build_net_user_command(password: str) -> str:
    # /Y answers the "longer than 14 characters" confirmation net.exe asks.
    return f"net user {USERNAME} {escape_for_cmd(password)} /Y"


def service_os_type(db: Session, service: Service, server: Server) -> Optional[str]:
    """``os_type`` of the template last installed for this service/server."""
    cfg = service.config if isinstance(service.config, dict) else {}
    template_id = cfg.get("template_id")
    if not template_id:
        tasks = InstallationTaskDAO.get_by_server(db, server.id)
        template_id = next((t.template_id for t in tasks if t.template_id), None)
    if not template_id:
        return None
    template = get_template_service().get_template(str(template_id))
    return (getattr(template, "os_type", None) or "").lower() or None


def windows_password_reset_ready(db: Session, service: Service, server: Optional[Server]) -> bool:
    """True when the action can be offered (ignores client permissions)."""
    if service.service_type != ServiceType.BARE_METAL or server is None or not sol_ready(server):
        return False
    try:
        return service_os_type(db, service, server) == "windows"
    except Exception:  # noqa: BLE001
        logger.debug("OS type lookup failed for service %s", service.id, exc_info=True)
        return False


async def _wait_for(hub: SolHub, mark: int, pattern: re.Pattern, timeout: float) -> bytes:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        output = hub.raw_since(mark)
        if pattern.search(output):
            return output
        if hub.closed:
            raise WindowsPasswordResetError("Serial-over-LAN session closed unexpectedly", 502)
        if loop.time() >= deadline:
            return output
        await asyncio.sleep(0.05)


async def _await_prompt(hub: SolHub, owner: object) -> None:
    # First wake-up may land in the getty's 2 s "ignore blank lines" window,
    # so try twice. Ctrl-U clears anything a viewer left half-typed.
    for payload in (b"\x15\r", b"\r"):
        mark = hub.raw_mark()
        await hub.inject(payload, owner=owner)
        if PROMPT_RE.search(await _wait_for(hub, mark, PROMPT_RE, PROMPT_ATTEMPT_SECONDS)):
            await asyncio.sleep(POST_PROMPT_DELAY_SECONDS)
            return
    raise WindowsPasswordResetError(
        "Serial admin console not available: no Administrator command prompt on the "
        "serial console. Windows may still be starting, or the console only starts "
        "after a logon on this install; reboot or log on once, then try again.",
        409,
    )


async def _run_line(hub: SolHub, owner: object, line: str, timeout: float) -> Optional[bytes]:
    """Send one command line; return its output up to the next prompt, or None on timeout."""
    mark = hub.raw_mark()
    # Leading Ctrl-U is sacrificial: the getty may swallow one byte after a CR.
    await hub.inject(b"\x15" + line.encode("ascii") + b"\r", owner=owner)
    output = await _wait_for(hub, mark, PROMPT_RE, timeout)
    match = PROMPT_RE.search(output)
    if not match:
        return None
    return output[: match.start()]


def _error_text(output: bytes, secret_values: list[str]) -> str:
    text = output.decode("utf-8", "replace")
    for value in secret_values:
        if value:
            text = text.replace(value, "********")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    lines = [ln for ln in lines if not ln.upper().startswith("MORE HELP IS AVAILABLE")]
    return " ".join(lines)[:500]


async def reset_windows_admin_password(
    db: Session,
    service: Service,
    server: Optional[Server],
    password: Optional[str] = None,
    *,
    source: str,
) -> dict:
    """Set the Windows Administrator password through the SOL serial console.

    Returns ``{"status": "ok", "username": ..., "generated": bool}`` plus
    ``password`` when RackFlow generated it. Raises
    :class:`WindowsPasswordResetError` with a caller-safe ``detail``.
    """
    if service.service_type != ServiceType.BARE_METAL:
        raise WindowsPasswordResetError("Only bare-metal services support this action", 400)
    if server is None:
        raise WindowsPasswordResetError("Service has no linked server", 404)
    if not sol_ready(server):
        raise WindowsPasswordResetError("Serial-over-LAN is not configured for this server", 409)
    if service_os_type(db, service, server) != "windows":
        raise WindowsPasswordResetError("The installed operating system is not Windows", 409)

    generated = not password
    if generated:
        password = generate_windows_password()
    validate_windows_password(password)
    escaped = escape_for_cmd(password)
    secret_values = [password, escaped]

    try:
        await _reset_over_sol(server, password, secret_values)
    except (WindowsPasswordResetError, SolUnavailable) as exc:
        detail = exc.detail if getattr(exc, "detail", None) else "Serial-over-LAN is unavailable"
        log_server_activity_failure(
            db,
            service_id=service.id,
            event_type=ServerActivityEventType.SERVICE,
            action=ACTION,
            source=source,
            message="Windows Administrator password reset failed",
            details={"server_id": server.id, "error": detail, "generated": generated},
        )
        if isinstance(exc, SolUnavailable):
            lower = detail.lower()
            code = 409 if ("busy" in lower or "another rackflow worker" in lower) else 502
            raise WindowsPasswordResetError(f"Serial-over-LAN unavailable: {detail}", code) from None
        raise

    cfg = dict(service.config or {}) if isinstance(service.config, dict) else {}
    params = dict(cfg.get("template_parameters") or {})
    params["admin_password"] = password
    cfg["template_parameters"] = params
    service.config = cfg
    flag_modified(service, "config")
    ServiceDAO.update(db, service)

    log_server_activity_success(
        db,
        service_id=service.id,
        event_type=ServerActivityEventType.SERVICE,
        action=ACTION,
        source=source,
        message="Reset Windows Administrator password over serial console",
        details={"server_id": server.id, "username": USERNAME, "generated": generated},
    )
    result = {"status": "ok", "username": USERNAME, "generated": generated}
    if generated:
        result["password"] = password
    return result


async def _reset_over_sol(server: Server, password: str, secret_values: list[str]) -> None:
    hub = await ensure_hub(server)
    async with hub.exclusive([v.encode("ascii") for v in secret_values]) as owner:
        await _await_prompt(hub, owner)
        output = await _run_line(hub, owner, build_net_user_command(password), COMMAND_TIMEOUT_SECONDS)
        if output is None:
            raise WindowsPasswordResetError(
                "Timed out waiting for net user to finish on the serial console; the "
                "password may or may not have changed. Try again or use the console.",
                504,
            )
        # Prompt is back: scrub the getty log whether net user succeeded or not.
        try:
            await asyncio.sleep(POST_PROMPT_DELAY_SECONDS)
            await _run_line(hub, owner, LOG_SCRUB_COMMAND, LOG_SCRUB_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001
            logger.warning("Could not scrub serial getty log on server %s", server.id)
        if not SUCCESS_RE.search(output):
            message = _error_text(output, secret_values) or "no output"
            raise WindowsPasswordResetError(f"net user failed: {message}", 422)
