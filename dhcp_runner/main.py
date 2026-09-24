"""
DHCP runner: small API to start/stop/restart dhcpd in its own container.
Main DCIM app calls this API. Config via env:
  DHCP_CONFIG_PATH, DHCP_LEASE_PATH, DHCP_INTERFACES
  API_KEY - if set, all endpoints except /health require X-API-Key or Authorization: Bearer
Auto-starts dhcpd on container startup if config file exists.
"""
import asyncio
import hmac
import os
import re
import logging
from collections import deque
from pathlib import Path
from typing import Optional

from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Header, Depends, Request

# Log buffer for GET /logs (last 500 lines)
LOG_BUFFER: deque = deque(maxlen=500)


class LogBufferHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            LOG_BUFFER.append(msg)
        except Exception:
            pass


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)
handler = LogBufferHandler()
handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(handler)

API_KEY = os.environ.get("API_KEY")
# Fail-closed by default: without an API_KEY the runner refuses all protected
# requests. Set ALLOW_UNAUTHENTICATED=true ONLY for isolated local development.
ALLOW_UNAUTHENTICATED = os.environ.get("ALLOW_UNAUTHENTICATED", "").lower() in ("1", "true", "yes")
DHCP_CONFIG_PATH = os.environ.get("DHCP_CONFIG_PATH", "/shared/dhcp/dhcpd.conf")
DHCP_LEASE_PATH = os.environ.get("DHCP_LEASE_PATH", "/shared/dhcp/dhcpd.leases")
DHCP_INTERFACES_ENV = os.environ.get("DHCP_INTERFACES", "eth0")
DHCPD_BINARY = "/usr/sbin/dhcpd"


# Linux network interface names are at most 15 bytes (IFNAMSIZ-1) and never
# start with '-'; enforcing this also blocks smuggling extra dhcpd argv flags
# (e.g. an "interface" of "-cf" or "/etc/passwd") through X-Runner-Interfaces,
# since argv is list-form/no shell but dhcpd itself still parses each element.
_VALID_INTERFACE_RE = re.compile(r"^[A-Za-z0-9_.]{1,15}$")


def _sanitize_interfaces(raw_names) -> list:
    valid = []
    for name in raw_names:
        name = name.strip()
        if not name:
            continue
        if not _VALID_INTERFACE_RE.match(name):
            logger.warning("Ignoring invalid interface name: %r", name)
            continue
        valid.append(name)
    return valid


def _get_interfaces() -> list:
    """Use runner_interfaces file (written by app) if present, else env."""
    interfaces_file = Path(DHCP_CONFIG_PATH).parent / "runner_interfaces"
    if interfaces_file.exists():
        try:
            raw = interfaces_file.read_text().strip()
            if raw:
                sanitized = _sanitize_interfaces(raw.splitlines())
                if sanitized:
                    return sanitized
        except Exception as e:
            logger.warning("Failed to read runner_interfaces: %s", e)
    return _sanitize_interfaces(DHCP_INTERFACES_ENV.split(",")) or ["eth0"]

_process: Optional[asyncio.subprocess.Process] = None
_lock = asyncio.Lock()
_agent = None


def _current_state() -> dict:
    running = _process is not None and _process.returncode is None
    return {
        "running": running,
        "status": "running" if running else "stopped",
        "pid": _process.pid if running else None,
        "config_path": DHCP_CONFIG_PATH,
        "lease_path": DHCP_LEASE_PATH,
        "interfaces": _get_interfaces(),
    }


async def _publish_state() -> None:
    if _agent is not None:
        await _agent.publish_state(_current_state(), force=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Auto-start dhcpd on container startup if config file exists."""
    global _agent
    uplink_task = None
    if Path(DHCP_CONFIG_PATH).exists():
        try:
            await start_dhcp()
            logger.info("DHCP auto-started on startup")
        except Exception as e:
            logger.warning("DHCP auto-start on startup skipped: %s", e)
    else:
        logger.info("No config file at %s; DHCP will start when config is saved and /start is called", DHCP_CONFIG_PATH)
    try:
        from runner_common.agent import agent_from_env

        _agent = agent_from_env(["dhcp"], _current_state, agent_version="1.0")
        if _agent:
            _agent.register("dhcp.start", _rpc_start)
            _agent.register("dhcp.stop", _rpc_stop)
            _agent.register("dhcp.restart", _rpc_restart)
            _agent.register("dhcp.status", _rpc_status)
            _agent.register("dhcp.get_config", _rpc_get_config)
            _agent.register("dhcp.put_config", _rpc_put_config)
            _agent.register("dhcp.logs", _rpc_logs)
            uplink_task = asyncio.create_task(_agent.run_forever())
            logger.info("DHCP runner uplink enabled")
    except Exception as e:
        logger.warning("DHCP runner uplink not started: %s", e)
    yield
    if _agent is not None:
        _agent.stop()
    if uplink_task is not None:
        uplink_task.cancel()
        try:
            await uplink_task
        except (asyncio.CancelledError, Exception):
            pass
    # Shutdown: stop if running
    if _process is not None and _process.returncode is None:
        _process.terminate()
        try:
            await asyncio.wait_for(_process.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            _process.kill()
        logger.info("DHCP stopped on shutdown")


app = FastAPI(title="DHCP Runner", version="1.0", lifespan=lifespan)


def _require_api_key(
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    authorization: Optional[str] = Header(None),
) -> None:
    """Require a valid API key. Fail closed if none is configured."""
    if not API_KEY:
        if ALLOW_UNAUTHENTICATED:
            return
        raise HTTPException(
            status_code=503,
            detail="Runner API_KEY is not configured; refusing requests. "
                   "Set API_KEY (or ALLOW_UNAUTHENTICATED=true for local dev).",
        )
    token = x_api_key
    if not token and authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
    if not token or not hmac.compare_digest(token, API_KEY):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def _ensure_dirs():
    Path(DHCP_CONFIG_PATH).parent.mkdir(parents=True, exist_ok=True)
    Path(DHCP_LEASE_PATH).parent.mkdir(parents=True, exist_ok=True)
    if not Path(DHCP_LEASE_PATH).exists():
        Path(DHCP_LEASE_PATH).touch()


@app.get("/config")
async def get_config(_: None = Depends(_require_api_key)):
    """Read dhcpd.conf file content."""
    p = Path(DHCP_CONFIG_PATH)
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"Config file not found: {DHCP_CONFIG_PATH}")
    return {"content": p.read_text(), "path": str(p)}


@app.put("/config")
async def put_config(request: Request, _: None = Depends(_require_api_key)):
    """Write dhcpd.conf file content. Body is raw text. Optional header X-Runner-Interfaces: eth0,eth1."""
    content = await request.body()
    p = Path(DHCP_CONFIG_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    interfaces_hdr = request.headers.get("X-Runner-Interfaces")
    if interfaces_hdr:
        raw_names = interfaces_hdr.replace(",", "\n").replace(" ", "\n").split("\n")
        sanitized = _sanitize_interfaces(raw_names)
        if sanitized:
            interfaces_file = p.parent / "runner_interfaces"
            interfaces_file.write_text("\n".join(sanitized) + "\n")
    return {"success": True, "path": str(p)}


@app.get("/logs")
async def get_logs(limit: int = 100, _: None = Depends(_require_api_key)):
    """Return recent log lines (last N)."""
    lines = list(LOG_BUFFER)[-limit:]
    return {"lines": lines, "count": len(lines)}


@app.post("/start")
async def start(auth: None = Depends(_require_api_key)):
    return await start_dhcp()


async def start_dhcp():
    global _process
    async with _lock:
        if _process is not None and _process.returncode is None:
            return {"success": True, "status": "running", "message": "DHCP already running", "pid": _process.pid}
        if not Path(DHCP_CONFIG_PATH).exists():
            raise HTTPException(status_code=400, detail=f"Config file not found: {DHCP_CONFIG_PATH}")
        _ensure_dirs()
        interfaces = _get_interfaces()
        cmd = [DHCPD_BINARY, "-f", "-q", "-cf", DHCP_CONFIG_PATH, "-lf", DHCP_LEASE_PATH] + interfaces
        try:
            _process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            logger.info("DHCP started PID %s", _process.pid)
            await _publish_state()
            return {"success": True, "status": "running", "message": "DHCP started", "pid": _process.pid}
        except Exception as e:
            logger.exception("DHCP start failed: %s", e)
            raise HTTPException(status_code=500, detail=str(e))


@app.post("/stop")
async def stop(auth: None = Depends(_require_api_key)):
    return await stop_dhcp()


async def stop_dhcp():
    global _process
    async with _lock:
        if _process is None or _process.returncode is not None:
            await _publish_state()
            return {"success": True, "status": "stopped", "message": "DHCP already stopped"}
        _process.terminate()
        try:
            await asyncio.wait_for(_process.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            _process.kill()
            await _process.wait()
        _process = None
        logger.info("DHCP stopped")
        await _publish_state()
        return {"success": True, "status": "stopped", "message": "DHCP stopped"}


@app.post("/restart")
async def restart(auth: None = Depends(_require_api_key)):
    return await restart_dhcp()


async def restart_dhcp():
    await stop_dhcp()
    await asyncio.sleep(1)
    return await start_dhcp()


@app.post("/reload")
async def reload(auth: None = Depends(_require_api_key)):
    return await restart_dhcp()


@app.get("/status")
async def status(auth: None = Depends(_require_api_key)):
    running = _process is not None and _process.returncode is None
    return {
        "status": "running" if running else "stopped",
        "pid": _process.pid if _process else None,
        "running": running,
        "config_path": DHCP_CONFIG_PATH,
        "lease_path": DHCP_LEASE_PATH,
        "interfaces": _get_interfaces(),
    }


@app.get("/health")
async def health():
    return {"ok": True}


def _rpc_error(exc: HTTPException) -> None:
    raise RuntimeError(str(exc.detail))


async def _rpc_start(params):
    del params
    try:
        return await start_dhcp()
    except HTTPException as exc:
        _rpc_error(exc)


async def _rpc_stop(params):
    del params
    return await stop_dhcp()


async def _rpc_restart(params):
    del params
    try:
        return await restart_dhcp()
    except HTTPException as exc:
        _rpc_error(exc)


async def _rpc_status(params):
    del params
    return _current_state()


async def _rpc_get_config(params):
    del params
    p = Path(DHCP_CONFIG_PATH)
    if not p.exists():
        raise RuntimeError(f"Config file not found: {DHCP_CONFIG_PATH}")
    return {"content": p.read_text(), "path": str(p)}


async def _rpc_put_config(params):
    content = params.get("content")
    if content is None and params.get("content_b64"):
        import base64
        content = base64.b64decode(params["content_b64"]).decode("utf-8")
    if content is None:
        raise RuntimeError("content is required")
    p = Path(DHCP_CONFIG_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content if isinstance(content, str) else content.decode("utf-8"))
    interfaces = params.get("interfaces")
    if isinstance(interfaces, str):
        raw_names = interfaces.replace(",", "\n").replace(" ", "\n").split("\n")
        sanitized = _sanitize_interfaces(raw_names)
        if sanitized:
            (p.parent / "runner_interfaces").write_text("\n".join(sanitized) + "\n")
    await _publish_state()
    return {"success": True, "path": str(p)}


async def _rpc_logs(params):
    try:
        limit = int(params.get("limit") or 100)
    except (TypeError, ValueError):
        limit = 100
    lines = list(LOG_BUFFER)[-limit:]
    return {"lines": lines, "count": len(lines)}
