"""
Small API server that runs in its own container and controls dhcpd and in.tftpd.
The main DCIM app calls this API to start/stop/restart DHCP and TFTP.

Config via env:
  DHCP_CONFIG_PATH   - path to dhcpd.conf (e.g. /shared/dhcp/dhcpd.conf)
  DHCP_LEASE_PATH     - path to dhcpd.leases
  DHCP_INTERFACES    - comma-separated interfaces (e.g. eth0,eth1)
  TFTP_ROOT          - TFTP root directory (e.g. /shared/tftp)
  TFTP_BIND          - bind address:port (e.g. 0.0.0.0:69)
  TFTP_IPV4_ONLY     - 1 for IPv4 only
  TFTP_ALLOW_CREATE  - 1 to allow file creation
"""
import asyncio
import os
import logging
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="DHCP/TFTP Runner", version="1.0")

# Env-based config
DHCP_CONFIG_PATH = os.environ.get("DHCP_CONFIG_PATH", "/shared/dhcp/dhcpd.conf")
DHCP_LEASE_PATH = os.environ.get("DHCP_LEASE_PATH", "/shared/dhcp/dhcpd.leases")
DHCP_INTERFACES = [x.strip() for x in os.environ.get("DHCP_INTERFACES", "eth0").split(",") if x.strip()]
TFTP_ROOT = os.environ.get("TFTP_ROOT", "/shared/tftp")
TFTP_BIND = os.environ.get("TFTP_BIND", "0.0.0.0:69")
TFTP_IPV4_ONLY = os.environ.get("TFTP_IPV4_ONLY", "1") == "1"
TFTP_ALLOW_CREATE = os.environ.get("TFTP_ALLOW_CREATE", "1") == "1"

_dhcp_process: Optional[asyncio.subprocess.Process] = None
_tftp_process: Optional[asyncio.subprocess.Process] = None
_dhcp_lock = asyncio.Lock()
_tftp_lock = asyncio.Lock()

DHCPD_BINARY = "/usr/sbin/dhcpd"
TFTPD_BINARY = "/usr/sbin/in.tftpd"


def _ensure_dirs():
    Path(DHCP_CONFIG_PATH).parent.mkdir(parents=True, exist_ok=True)
    Path(DHCP_LEASE_PATH).parent.mkdir(parents=True, exist_ok=True)
    Path(TFTP_ROOT).mkdir(parents=True, exist_ok=True)
    if not Path(DHCP_LEASE_PATH).exists():
        Path(DHCP_LEASE_PATH).touch()


# --- DHCP ---

@app.post("/dhcp/start")
async def dhcp_start():
    """Start dhcpd (foreground subprocess)."""
    global _dhcp_process
    async with _dhcp_lock:
        if _dhcp_process is not None and _dhcp_process.returncode is None:
            return {"success": True, "status": "running", "message": "DHCP already running", "pid": _dhcp_process.pid}
        if not Path(DHCP_CONFIG_PATH).exists():
            raise HTTPException(status_code=400, detail=f"Config file not found: {DHCP_CONFIG_PATH}")
        _ensure_dirs()
        cmd = [DHCPD_BINARY, "-f", "-q", "-cf", DHCP_CONFIG_PATH, "-lf", DHCP_LEASE_PATH] + DHCP_INTERFACES
        try:
            _dhcp_process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            logger.info("DHCP started PID %s", _dhcp_process.pid)
            return {"success": True, "status": "running", "message": "DHCP started", "pid": _dhcp_process.pid}
        except Exception as e:
            logger.exception("DHCP start failed: %s", e)
            raise HTTPException(status_code=500, detail=str(e))


@app.post("/dhcp/stop")
async def dhcp_stop():
    """Stop dhcpd."""
    global _dhcp_process
    async with _dhcp_lock:
        if _dhcp_process is None or _dhcp_process.returncode is not None:
            return {"success": True, "status": "stopped", "message": "DHCP already stopped"}
        _dhcp_process.terminate()
        try:
            await asyncio.wait_for(_dhcp_process.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            _dhcp_process.kill()
            await _dhcp_process.wait()
        _dhcp_process = None
        logger.info("DHCP stopped")
        return {"success": True, "status": "stopped", "message": "DHCP stopped"}


@app.post("/dhcp/restart")
async def dhcp_restart():
    """Restart dhcpd."""
    await dhcp_stop()
    await asyncio.sleep(1)
    return await dhcp_start()


@app.post("/dhcp/reload")
async def dhcp_reload():
    """Reload config (restart)."""
    return await dhcp_restart()


@app.get("/dhcp/status")
async def dhcp_status():
    running = _dhcp_process is not None and _dhcp_process.returncode is None
    return {
        "status": "running" if running else "stopped",
        "pid": _dhcp_process.pid if _dhcp_process else None,
        "running": running,
        "config_path": DHCP_CONFIG_PATH,
        "lease_path": DHCP_LEASE_PATH,
        "interfaces": DHCP_INTERFACES,
    }


# --- TFTP ---

@app.post("/tftp/start")
async def tftp_start():
    """Start in.tftpd (foreground subprocess)."""
    global _tftp_process
    async with _tftp_lock:
        if _tftp_process is not None and _tftp_process.returncode is None:
            return {"success": True, "status": "running", "message": "TFTP already running", "pid": _tftp_process.pid}
        _ensure_dirs()
        cmd = [TFTPD_BINARY, "-L", "-s", TFTP_ROOT, "-a", TFTP_BIND]
        if TFTP_IPV4_ONLY:
            cmd.insert(1, "-4")
        if TFTP_ALLOW_CREATE:
            cmd.append("-c")
        cmd.append("-v")
        try:
            _tftp_process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            logger.info("TFTP started PID %s", _tftp_process.pid)
            return {"success": True, "status": "running", "message": "TFTP started", "pid": _tftp_process.pid}
        except Exception as e:
            logger.exception("TFTP start failed: %s", e)
            raise HTTPException(status_code=500, detail=str(e))


@app.post("/tftp/stop")
async def tftp_stop():
    """Stop in.tftpd."""
    global _tftp_process
    async with _tftp_lock:
        if _tftp_process is None or _tftp_process.returncode is not None:
            return {"success": True, "status": "stopped", "message": "TFTP already stopped"}
        _tftp_process.terminate()
        try:
            await asyncio.wait_for(_tftp_process.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            _tftp_process.kill()
            await _tftp_process.wait()
        _tftp_process = None
        logger.info("TFTP stopped")
        return {"success": True, "status": "stopped", "message": "TFTP stopped"}


@app.post("/tftp/restart")
async def tftp_restart():
    """Restart TFTP."""
    await tftp_stop()
    await asyncio.sleep(1)
    return await tftp_start()


@app.get("/tftp/status")
async def tftp_status():
    running = _tftp_process is not None and _tftp_process.returncode is None
    return {
        "status": "running" if running else "stopped",
        "pid": _tftp_process.pid if _tftp_process else None,
        "running": running,
        "root_directory": TFTP_ROOT,
        "bind": TFTP_BIND,
    }


@app.get("/health")
async def health():
    return {"ok": True}
