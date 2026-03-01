"""
DHCP runner: small API to start/stop/restart dhcpd in its own container.
Main DCIM app calls this API. Config via env:
  DHCP_CONFIG_PATH, DHCP_LEASE_PATH, DHCP_INTERFACES
  API_KEY - if set, all endpoints except /health require X-API-Key or Authorization: Bearer
Auto-starts dhcpd on container startup if config file exists.
"""
import asyncio
import os
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
DHCP_CONFIG_PATH = os.environ.get("DHCP_CONFIG_PATH", "/shared/dhcp/dhcpd.conf")
DHCP_LEASE_PATH = os.environ.get("DHCP_LEASE_PATH", "/shared/dhcp/dhcpd.leases")
DHCP_INTERFACES_ENV = os.environ.get("DHCP_INTERFACES", "eth0")
DHCPD_BINARY = "/usr/sbin/dhcpd"


def _get_interfaces() -> list:
    """Use runner_interfaces file (written by app) if present, else env."""
    interfaces_file = Path(DHCP_CONFIG_PATH).parent / "runner_interfaces"
    if interfaces_file.exists():
        try:
            raw = interfaces_file.read_text().strip()
            if raw:
                return [x.strip() for x in raw.splitlines() if x.strip()]
        except Exception as e:
            logger.warning("Failed to read runner_interfaces: %s", e)
    return [x.strip() for x in DHCP_INTERFACES_ENV.split(",") if x.strip()] or ["eth0"]

_process: Optional[asyncio.subprocess.Process] = None
_lock = asyncio.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Auto-start dhcpd on container startup if config file exists."""
    if Path(DHCP_CONFIG_PATH).exists():
        try:
            await start()
            logger.info("DHCP auto-started on startup")
        except Exception as e:
            logger.warning("DHCP auto-start on startup skipped: %s", e)
    else:
        logger.info("No config file at %s; DHCP will start when config is saved and /start is called", DHCP_CONFIG_PATH)
    yield
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
    """Require API key if API_KEY env is set."""
    if not API_KEY:
        return
    token = x_api_key
    if not token and authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
    if not token or token != API_KEY:
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
        interfaces_file = p.parent / "runner_interfaces"
        interfaces_file.write_text(interfaces_hdr.replace(",", "\n").replace(" ", "\n").replace("\n\n", "\n").strip() + "\n")
    return {"success": True, "path": str(p)}


@app.get("/logs")
async def get_logs(limit: int = 100, _: None = Depends(_require_api_key)):
    """Return recent log lines (last N)."""
    lines = list(LOG_BUFFER)[-limit:]
    return {"lines": lines, "count": len(lines)}


@app.post("/start")
async def start(auth: None = Depends(_require_api_key)):
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
            return {"success": True, "status": "running", "message": "DHCP started", "pid": _process.pid}
        except Exception as e:
            logger.exception("DHCP start failed: %s", e)
            raise HTTPException(status_code=500, detail=str(e))


@app.post("/stop")
async def stop(auth: None = Depends(_require_api_key)):
    global _process
    async with _lock:
        if _process is None or _process.returncode is not None:
            return {"success": True, "status": "stopped", "message": "DHCP already stopped"}
        _process.terminate()
        try:
            await asyncio.wait_for(_process.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            _process.kill()
            await _process.wait()
        _process = None
        logger.info("DHCP stopped")
        return {"success": True, "status": "stopped", "message": "DHCP stopped"}


@app.post("/restart")
async def restart(auth: None = Depends(_require_api_key)):
    await stop()
    await asyncio.sleep(1)
    return await start()


@app.post("/reload")
async def reload(auth: None = Depends(_require_api_key)):
    return await restart()


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
