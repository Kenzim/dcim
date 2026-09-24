"""
TFTP runner: small API to start/stop/restart in.tftpd in its own container.
Main DCIM app calls this API. Config via env:
  TFTP_ROOT, TFTP_BIND, TFTP_IPV4_ONLY, TFTP_ALLOW_CREATE
  API_KEY - if set, all endpoints except /health require X-API-Key or Authorization: Bearer
Auto-starts in.tftpd on container startup.
"""
import asyncio
import base64
import hmac
import logging
import os
import shutil
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
# Fail-closed by default: without an API_KEY the runner refuses all protected
# requests. Set ALLOW_UNAUTHENTICATED=true ONLY for isolated local development.
ALLOW_UNAUTHENTICATED = os.environ.get("ALLOW_UNAUTHENTICATED", "").lower() in ("1", "true", "yes")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Auto-start TFTP daemon on container startup."""
    global _agent
    uplink_task = None
    try:
        await start_tftp()
        logger.info("TFTP auto-started on startup")
    except Exception as e:
        logger.warning("TFTP auto-start on startup skipped: %s", e)
    try:
        from runner_common.agent import agent_from_env

        _agent = agent_from_env(["tftp"], _current_state, agent_version="1.0")
        if _agent:
            _agent.register("tftp.start", _rpc_start)
            _agent.register("tftp.stop", _rpc_stop)
            _agent.register("tftp.restart", _rpc_restart)
            _agent.register("tftp.status", _rpc_status)
            _agent.register("tftp.get_config", _rpc_get_config)
            _agent.register("tftp.put_config", _rpc_put_config)
            _agent.register("tftp.logs", _rpc_logs)
            uplink_task = asyncio.create_task(_agent.run_forever())
            logger.info("TFTP runner uplink enabled")
    except Exception as e:
        logger.warning("TFTP runner uplink not started: %s", e)
    yield
    if _agent is not None:
        _agent.stop()
    if uplink_task is not None:
        uplink_task.cancel()
        try:
            await uplink_task
        except (asyncio.CancelledError, Exception):
            pass
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


TFTP_ROOT = os.environ.get("TFTP_ROOT", "/shared/tftp")
TFTP_BIND = os.environ.get("TFTP_BIND", "0.0.0.0:69")
TFTP_IPV4_ONLY = os.environ.get("TFTP_IPV4_ONLY", "1") == "1"
TFTP_ALLOW_CREATE = os.environ.get("TFTP_ALLOW_CREATE", "1") == "1"
TFTPD_BINARY = "/usr/sbin/in.tftpd"

_process: Optional[asyncio.subprocess.Process] = None
_lock = asyncio.Lock()
_agent = None


def _current_state() -> dict:
    running = _process is not None and _process.returncode is None
    return {
        "running": running,
        "status": "running" if running else "stopped",
        "pid": _process.pid if running else None,
        "root_directory": TFTP_ROOT,
        "bind": TFTP_BIND,
    }


async def _publish_state() -> None:
    if _agent is not None:
        await _agent.publish_state(_current_state(), force=True)


def ensure_bios_ipxe_at_root(root: Optional[str] = None) -> None:
    """Copy pxe/undionly.kpxe to the TFTP chroot root.

    Intel Boot Agent (BIOS PXE on add-in NICs) hangs or ignores subdirectory
    filenames such as pxe/undionly.kpxe. DHCP advertises undionly.kpxe.
    """
    base = Path(root or TFTP_ROOT)
    src = base / "pxe" / "undionly.kpxe"
    dest = base / "undionly.kpxe"
    if not src.is_file():
        return
    try:
        shutil.copy2(src, dest)
    except OSError as e:
        logger.warning("Failed to copy BIOS iPXE loader to TFTP root: %s", e)


def _ensure_dirs():
    Path(TFTP_ROOT).mkdir(parents=True, exist_ok=True)
    ensure_bios_ipxe_at_root()


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
    root = Path(TFTP_ROOT).resolve()
    target = (root / body.path).resolve()
    # Confine writes to the TFTP root. A string startswith() check is unsafe
    # (e.g. "/shared/tftp" is a prefix of "/shared/tftp-evil"); compare paths.
    if target != root and root not in target.parents:
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
    return await start_tftp()


async def start_tftp():
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
            await _publish_state()
            return {"success": True, "status": "running", "message": "TFTP started", "pid": _process.pid}
        except Exception as e:
            logger.exception("TFTP start failed: %s", e)
            raise HTTPException(status_code=500, detail=str(e))


@app.post("/stop")
async def stop(auth: None = Depends(_require_api_key)):
    return await stop_tftp()


async def stop_tftp():
    global _process
    async with _lock:
        if _process is None or _process.returncode is not None:
            await _publish_state()
            return {"success": True, "status": "stopped", "message": "TFTP already stopped"}
        _process.terminate()
        try:
            await asyncio.wait_for(_process.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            _process.kill()
            await _process.wait()
        _process = None
        logger.info("TFTP stopped")
        await _publish_state()
        return {"success": True, "status": "stopped", "message": "TFTP stopped"}


@app.post("/restart")
async def restart(auth: None = Depends(_require_api_key)):
    return await restart_tftp()


async def restart_tftp():
    await stop_tftp()
    await asyncio.sleep(1)
    return await start_tftp()


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


async def _rpc_start(params):
    del params
    try:
        return await start_tftp()
    except HTTPException as exc:
        raise RuntimeError(str(exc.detail)) from exc


async def _rpc_stop(params):
    del params
    return await stop_tftp()


async def _rpc_restart(params):
    del params
    try:
        return await restart_tftp()
    except HTTPException as exc:
        raise RuntimeError(str(exc.detail)) from exc


async def _rpc_status(params):
    del params
    return _current_state()


async def _rpc_get_config(params):
    del params
    root = Path(TFTP_ROOT)
    root.mkdir(parents=True, exist_ok=True)
    return {
        "root": TFTP_ROOT,
        "bind": TFTP_BIND,
        "listing": _list_dir_recursive(root, root),
    }


async def _rpc_put_config(params):
    rel = str(params.get("path") or "")
    content = params.get("content")
    root = Path(TFTP_ROOT).resolve()
    target = (root / rel).resolve()
    if target != root and root not in target.parents:
        raise RuntimeError("Path escapes TFTP root")
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        try:
            data = base64.b64decode(content)
        except Exception as e:
            raise RuntimeError(f"Invalid base64 content: {e}") from e
    elif isinstance(content, (bytes, bytearray)):
        data = bytes(content)
    else:
        raise RuntimeError("content is required")
    target.write_bytes(data)
    await _publish_state()
    return {"success": True, "path": rel}


async def _rpc_logs(params):
    try:
        limit = int(params.get("limit") or 100)
    except (TypeError, ValueError):
        limit = 100
    lines = list(LOG_BUFFER)[-limit:]
    return {"lines": lines, "count": len(lines)}
