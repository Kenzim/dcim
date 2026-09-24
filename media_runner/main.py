"""Per-location ISO library: HTTP range serving, SMB share, Rackflow uplink."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import secrets
import shutil
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlparse

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, Response

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

API_KEY = os.environ.get("API_KEY") or os.environ.get("RUNNER_API_KEY")
ALLOW_UNAUTHENTICATED = os.environ.get("ALLOW_UNAUTHENTICATED", "").lower() in ("1", "true", "yes")
MEDIA_ROOT = Path(os.environ.get("MEDIA_ROOT", "/data/isos"))
SMB_SHARE = os.environ.get("SMB_SHARE", "isos")
SMB_ADVERTISE_HOST = os.environ.get("SMB_ADVERTISE_HOST", "").strip()
PUBLIC_HTTP_BASE = os.environ.get("PUBLIC_HTTP_BASE", "").strip().rstrip("/")
HTTP_PORT = int(os.environ.get("HTTP_PORT", "9083"))
SEED_ISO = Path(os.environ.get("SEED_ISO", "/opt/seed/rackflow-netboot.iso"))
SMB_ENABLE = os.environ.get("SMB_ENABLE", "1").lower() not in ("0", "false", "no")

_INVALID_ISO = "Invalid ISO filename"
_agent = None
_smb_process: Optional[asyncio.subprocess.Process] = None
_jobs: dict[str, dict[str, Any]] = {}
_smb_creds: dict[str, dict[str, Any]] = {}
_lock = asyncio.Lock()
LOG_BUFFER: deque = deque(maxlen=200)


def _require_api_key(
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    authorization: Optional[str] = Header(None),
) -> None:
    if not API_KEY:
        if ALLOW_UNAUTHENTICATED:
            return
        raise HTTPException(status_code=503, detail="Runner API_KEY is not configured")
    token = x_api_key
    if not token and authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
    if not token or not hmac.compare_digest(token, API_KEY):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def validate_iso_filename(filename: str) -> str:
    name = unquote((filename or "").strip())
    if not name or name != Path(name).name:
        raise HTTPException(status_code=400, detail=_INVALID_ISO)
    if ".." in name or "/" in name or "\\" in name or "\x00" in name:
        raise HTTPException(status_code=400, detail=_INVALID_ISO)
    if not name.lower().endswith(".iso"):
        raise HTTPException(status_code=400, detail=_INVALID_ISO)
    return name


def iso_path(filename: str) -> Path:
    name = validate_iso_filename(filename)
    path = (MEDIA_ROOT / name).resolve()
    try:
        path.relative_to(MEDIA_ROOT.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_INVALID_ISO) from exc
    return path


def list_iso_files() -> list[dict[str, Any]]:
    MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for path in MEDIA_ROOT.iterdir():
        if not path.is_file() or path.suffix.lower() != ".iso":
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        rows.append(
            {
                "filename": path.name,
                "size_bytes": size,
                "size_mb": round(size / (1024 * 1024), 2),
            }
        )
    return sorted(rows, key=lambda row: row["filename"])


def disk_usage() -> dict[str, Any]:
    usage = shutil.disk_usage(MEDIA_ROOT)
    return {"total_bytes": usage.total, "used_bytes": usage.used, "free_bytes": usage.free}


def advertised_http_base() -> str:
    if PUBLIC_HTTP_BASE:
        return PUBLIC_HTTP_BASE
    host = SMB_ADVERTISE_HOST or "127.0.0.1"
    return f"http://{host}:{HTTP_PORT}"


def advertised_smb_host() -> str:
    return SMB_ADVERTISE_HOST or "127.0.0.1"


def current_state() -> dict[str, Any]:
    jobs = [dict(job) for job in _jobs.values()]
    return {
        "running": True,
        "status": "running",
        "public_http_base": advertised_http_base(),
        "smb_host": advertised_smb_host(),
        "smb_share": SMB_SHARE,
        "isos": list_iso_files(),
        "jobs": jobs,
        "disk": disk_usage(),
    }


async def _publish_state() -> None:
    if _agent is not None:
        await _agent.publish_state(current_state(), force=True)


def seed_netboot() -> None:
    MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
    dest = MEDIA_ROOT / "rackflow-netboot.iso"
    if dest.exists() or not SEED_ISO.is_file():
        return
    try:
        shutil.copy2(SEED_ISO, dest)
        logger.info("Seeded %s", dest)
    except OSError as exc:
        logger.warning("Could not seed netboot ISO: %s", exc)


async def start_smbd() -> None:
    global _smb_process
    if not SMB_ENABLE:
        return
    MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
    if _smb_process is not None and _smb_process.returncode is None:
        return
    try:
        _smb_process = await asyncio.create_subprocess_exec(
            "smbd",
            "-F",
            "-S",
            "--no-process-group",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        logger.info("smbd started PID %s", _smb_process.pid)
    except Exception as exc:  # noqa: BLE001
        logger.warning("smbd failed to start: %s", exc)
        _smb_process = None


async def _add_samba_user(username: str, password: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        "id", username, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )
    await proc.wait()
    if proc.returncode != 0:
        create = await asyncio.create_subprocess_exec(
            "useradd", "-M", "-s", "/usr/sbin/nologin", username
        )
        await create.wait()
        if create.returncode not in (0, 9):
            raise RuntimeError("Could not create SMB user")
    proc = await asyncio.create_subprocess_exec(
        "smbpasswd", "-a", "-s", username,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    payload = f"{password}\n{password}\n".encode()
    _, err = await proc.communicate(payload)
    if proc.returncode != 0:
        raise RuntimeError(err.decode("utf-8", errors="replace") or "smbpasswd failed")


async def _delete_samba_user(username: str) -> None:
    try:
        proc = await asyncio.create_subprocess_exec("smbpasswd", "-x", username)
        await proc.wait()
    except Exception:  # noqa: BLE001
        pass
    try:
        proc = await asyncio.create_subprocess_exec("userdel", username)
        await proc.wait()
    except Exception:  # noqa: BLE001
        pass


async def expire_smb_loop() -> None:
    while True:
        await asyncio.sleep(30)
        now = time.time()
        expired = [user for user, meta in list(_smb_creds.items()) if meta.get("expires_at", 0) <= now]
        for user in expired:
            await _delete_samba_user(user)
            _smb_creds.pop(user, None)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _agent
    seed_netboot()
    await start_smbd()
    expire_task = asyncio.create_task(expire_smb_loop())
    uplink_task = None
    try:
        from runner_common.agent import agent_from_env

        extra = {
            "public_http_base": advertised_http_base(),
            "smb_host": advertised_smb_host(),
            "smb_share": SMB_SHARE,
        }
        _agent = agent_from_env(["media"], current_state, agent_version="1.0", hello_extra=extra)
        if _agent:
            _agent.register("media.list", rpc_list)
            _agent.register("media.download", rpc_download)
            _agent.register("media.upload_url", rpc_upload_url)
            _agent.register("media.delete", rpc_delete)
            _agent.register("media.mint_smb_credentials", rpc_mint_smb)
            _agent.register("media.disk_usage", rpc_disk)
            uplink_task = asyncio.create_task(_agent.run_forever())
            logger.info("Media runner uplink enabled")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Media runner uplink not started: %s", exc)
    yield
    if _agent is not None:
        _agent.stop()
    expire_task.cancel()
    if uplink_task is not None:
        uplink_task.cancel()
    if _smb_process is not None and _smb_process.returncode is None:
        _smb_process.terminate()


app = FastAPI(title="Media Runner", version="1.0", lifespan=lifespan)


def _parse_byte_range(range_header: str, file_size: int) -> tuple[int, int]:
    if not range_header.lower().startswith("bytes="):
        raise HTTPException(status_code=416)
    spec = range_header.split("=", 1)[1].strip()
    if "," in spec:
        raise HTTPException(status_code=416)
    start_s, _, end_s = spec.partition("-")
    try:
        if start_s == "":
            suffix = int(end_s)
            start = max(file_size - suffix, 0)
            end = file_size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else file_size - 1
    except ValueError as exc:
        raise HTTPException(status_code=416) from exc
    if start < 0 or start >= file_size or end < start:
        raise HTTPException(status_code=416, headers={"Content-Range": f"bytes */{file_size}"})
    return start, min(end, file_size - 1)


def _range_response(request: Request, path: Path, filename: str) -> Response:
    file_size = path.stat().st_size
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": "application/octet-stream",
        "Content-Disposition": f'inline; filename="{filename}"',
    }
    range_header = (request.headers.get("range") or "").strip()
    if not range_header:
        if request.method == "HEAD":
            return Response(status_code=200, headers={**headers, "Content-Length": str(file_size)})
        return FileResponse(
            path,
            media_type="application/octet-stream",
            filename=filename,
            content_disposition_type="inline",
            headers={"Accept-Ranges": "bytes"},
        )
    start, end = _parse_byte_range(range_header, file_size)
    length = end - start + 1
    range_headers = {**headers, "Content-Length": str(length), "Content-Range": f"bytes {start}-{end}/{file_size}"}
    if request.method == "HEAD":
        return Response(status_code=206, headers=range_headers)
    with path.open("rb") as handle:
        handle.seek(start)
        data = handle.read(length)
    return Response(content=data, status_code=206, headers=range_headers)


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/status")
async def status(_: None = Depends(_require_api_key)):
    return current_state()


@app.api_route("/isos/{filename}", methods=["GET", "HEAD"])
async def serve_iso(filename: str, request: Request):
    path = iso_path(filename)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="ISO file not found")
    return _range_response(request, path, path.name)


def _filename_from_url(url: str, override: Optional[str]) -> str:
    if override:
        return validate_iso_filename(override)
    parsed = urlparse(url)
    name = Path(unquote(parsed.path or "")).name
    return validate_iso_filename(name)


def _update_job(job_id: str, **fields: Any) -> dict[str, Any]:
    job = _jobs.setdefault(job_id, {"id": job_id, "status": "queued", "percent": 0})
    job.update(fields)
    return job


async def _download_to(url: str, dest: Path, job_id: str, headers: Optional[dict] = None) -> None:
    tmp = dest.with_suffix(dest.suffix + ".part")
    _update_job(job_id, status="running", percent=0, filename=dest.name)
    await _publish_state()
    try:
        timeout = httpx.Timeout(None, connect=30.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers or {}) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("content-length") or 0)
                done = 0
                last_pub = 0.0
                with tmp.open("wb") as handle:
                    async for chunk in resp.aiter_bytes(256 * 1024):
                        handle.write(chunk)
                        done += len(chunk)
                        percent = int(done * 100 / total) if total else 0
                        _update_job(job_id, percent=percent, bytes_done=done, bytes_total=total)
                        now = time.monotonic()
                        if now - last_pub >= 1.0:
                            last_pub = now
                            if _agent is not None:
                                await _agent.publish_event("media.progress", _jobs[job_id])
        tmp.replace(dest)
        _update_job(job_id, status="done", percent=100)
    except Exception as exc:  # noqa: BLE001
        _update_job(job_id, status="error", error=str(exc))
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise
    finally:
        await _publish_state()


async def rpc_list(params):
    del params
    return {"isos": list_iso_files(), "disk": disk_usage()}


async def rpc_disk(params):
    del params
    return disk_usage()


async def rpc_delete(params):
    name = validate_iso_filename(str(params.get("filename") or ""))
    path = iso_path(name)
    if not path.is_file():
        raise RuntimeError(f"ISO file '{name}' not found")
    path.unlink()
    await _publish_state()
    return {"success": True, "filename": name}


async def rpc_download(params):
    url = str(params.get("url") or "").strip()
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        raise RuntimeError("url must be http(s)")
    name = _filename_from_url(url, params.get("filename"))
    dest = iso_path(name)
    job_id = hashlib.sha1(f"{url}:{name}:{time.time()}".encode()).hexdigest()[:12]
    _update_job(job_id, kind="download", filename=name, url=url, status="queued")
    asyncio.create_task(_download_to(url, dest, job_id))
    await _publish_state()
    return {"job_id": job_id, "filename": name, "status": "queued"}


async def rpc_upload_url(params):
    url = str(params.get("url") or "").strip()
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        raise RuntimeError("url must be http(s)")
    name = validate_iso_filename(str(params.get("filename") or Path(parsed.path).name))
    dest = iso_path(name)
    headers = {}
    if API_KEY:
        headers["X-API-Key"] = API_KEY
    job_id = hashlib.sha1(f"upload:{name}:{time.time()}".encode()).hexdigest()[:12]
    _update_job(job_id, kind="upload", filename=name, url=url, status="queued")
    asyncio.create_task(_download_to(url, dest, job_id, headers=headers))
    await _publish_state()
    return {"job_id": job_id, "filename": name, "status": "queued"}


async def rpc_mint_smb(params):
    name = validate_iso_filename(str(params.get("filename") or ""))
    path = iso_path(name)
    if not path.is_file():
        raise RuntimeError(f"ISO file '{name}' not found")
    try:
        ttl = max(60, int(params.get("ttl") or 14400))
    except (TypeError, ValueError):
        ttl = 14400
    username = f"vm{secrets.token_hex(4)}"
    password = secrets.token_urlsafe(18)
    async with _lock:
        await _add_samba_user(username, password)
        _smb_creds[username] = {"expires_at": time.time() + ttl, "filename": name}
    return {
        "host": advertised_smb_host(),
        "share": SMB_SHARE,
        "filename": name,
        "unc_path": f"\\{SMB_SHARE}\\{name}",
        "user": username,
        "password": password,
        "ttl": ttl,
    }
