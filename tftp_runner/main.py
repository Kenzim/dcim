"""
TFTP runner: small API to start/stop/restart in.tftpd in its own container.
Main DCIM app calls this API. Config via env:
  TFTP_ROOT, TFTP_BIND, TFTP_IPV4_ONLY, TFTP_ALLOW_CREATE
  API_KEY - if set, all endpoints except /health require X-API-Key or Authorization: Bearer
Auto-starts in.tftpd on container startup.
"""
import asyncio
import base64
import os
import logging
from collections import deque
from pathlib import Path
from typing import Optional, List, Dict, Any

from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Header, Depends
from pydantic import BaseModel

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Auto-start TFTP daemon on container startup."""
    try:
        await start()
        logger.info("TFTP auto-started on startup")
    except Exception as e:
        logger.warning("TFTP auto-start on startup skipped: %s", e)
    yield
    if _process is not None and _process.returncode is None:
        _process.terminate()
        try:
            await asyncio.wait_for(_process.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            _process.kill()
        logger.info("TFTP stopped on shutdown")


app = FastAPI(title="TFTP Runner", version="1.0", lifespan=lifespan)


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


TFTP_ROOT = os.environ.get("TFTP_ROOT", "/shared/tftp")
TFTP_BIND = os.environ.get("TFTP_BIND", "0.0.0.0:69")
TFTP_IPV4_ONLY = os.environ.get("TFTP_IPV4_ONLY", "1") == "1"
TFTP_ALLOW_CREATE = os.environ.get("TFTP_ALLOW_CREATE", "1") == "1"
TFTPD_BINARY = "/usr/sbin/in.tftpd"

_process: Optional[asyncio.subprocess.Process] = None
_lock = asyncio.Lock()


def _ensure_dirs():
    Path(TFTP_ROOT).mkdir(parents=True, exist_ok=True)


def _list_dir_recursive(root: Path, base: Path, max_depth: int = 3, depth: int = 0) -> List[Dict[str, Any]]:
    """List directory contents (name, path, is_dir, size). Limited depth."""
    items = []
    if depth >= max_depth:
        return items
    try:
        for p in sorted(root.iterdir()):
            rel = p.relative_to(base)
            items.append({
                "name": p.name,
                "path": str(rel),
                "is_dir": p.is_dir(),
                "size": p.stat().st_size if p.is_file() else None,
            })
            if p.is_dir():
                items[-1]["children"] = _list_dir_recursive(p, base, max_depth, depth + 1)
    except PermissionError:
        pass
    return items


@app.get("/config")
async def get_config(auth: None = Depends(_require_api_key)):
    """Return TFTP root info and directory listing."""
    root = Path(TFTP_ROOT)
    root.mkdir(parents=True, exist_ok=True)
    return {
        "root": TFTP_ROOT,
        "bind": TFTP_BIND,
        "listing": _list_dir_recursive(root, root),
    }


class ConfigWriteBody(BaseModel):
    path: str  # relative to TFTP root
    content: str  # base64-encoded content


@app.put("/config")
async def put_config(body: ConfigWriteBody, auth: None = Depends(_require_api_key)):
    """Write a file under TFTP root. Path is relative; content is base64."""
    root = Path(TFTP_ROOT)
    target = (root / body.path).resolve()
    if not str(target).startswith(str(root.resolve())):
        raise HTTPException(status_code=400, detail="Path escapes TFTP root")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = base64.b64decode(body.content)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid base64 content: {e}")
    target.write_bytes(data)
    return {"success": True, "path": body.path}


@app.get("/logs")
async def get_logs(limit: int = 100, auth: None = Depends(_require_api_key)):
    """Return recent log lines (last N)."""
    lines = list(LOG_BUFFER)[-limit:]
    return {"lines": lines, "count": len(lines)}


@app.post("/start")
async def start(auth: None = Depends(_require_api_key)):
    global _process
    async with _lock:
        if _process is not None and _process.returncode is None:
            return {"success": True, "status": "running", "message": "TFTP already running", "pid": _process.pid}
        _ensure_dirs()
        cmd = [TFTPD_BINARY, "-L", "-s", TFTP_ROOT, "-a", TFTP_BIND]
        if TFTP_IPV4_ONLY:
            cmd.insert(1, "-4")
        if TFTP_ALLOW_CREATE:
            cmd.append("-c")
        cmd.append("-v")
        try:
            _process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            logger.info("TFTP started PID %s", _process.pid)
            return {"success": True, "status": "running", "message": "TFTP started", "pid": _process.pid}
        except Exception as e:
            logger.exception("TFTP start failed: %s", e)
            raise HTTPException(status_code=500, detail=str(e))


@app.post("/stop")
async def stop(auth: None = Depends(_require_api_key)):
    global _process
    async with _lock:
        if _process is None or _process.returncode is not None:
            return {"success": True, "status": "stopped", "message": "TFTP already stopped"}
        _process.terminate()
        try:
            await asyncio.wait_for(_process.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            _process.kill()
            await _process.wait()
        _process = None
        logger.info("TFTP stopped")
        return {"success": True, "status": "stopped", "message": "TFTP stopped"}


@app.post("/restart")
async def restart(auth: None = Depends(_require_api_key)):
    await stop()
    await asyncio.sleep(1)
    return await start()


@app.get("/status")
async def status(auth: None = Depends(_require_api_key)):
    running = _process is not None and _process.returncode is None
    return {
        "status": "running" if running else "stopped",
        "pid": _process.pid if _process else None,
        "running": running,
        "root_directory": TFTP_ROOT,
        "bind": TFTP_BIND,
    }


@app.get("/health")
async def health():
    return {"ok": True}
