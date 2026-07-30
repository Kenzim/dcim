"""
Proxmox plugin for VM management via Proxmox VE API.

Uses Proxmox REST API for VM power control.
"""
import asyncio
import logging
import re
import shlex
import urllib.parse
from typing import Awaitable, Callable, Dict, Any, List, Optional, Sequence

import httpx
from app.plugins.base import (
    ServerPlugin,
    PluginCategory,
    PowerState,
)
from app.plugins.capabilities import Capability, ActionDef, UIPattern

logger = logging.getLogger(__name__)

_SERIAL_DEVICE_KEY_RE = re.compile(r"^serial\d+$")
# UPID:<node>:<hexpid>:<hexpstart>:<hexstarttime>:<type>:<id>:<user>:
_UPID_NODE_RE = re.compile(r"^UPID:([^:]+):")


def proxmox_node_from_upid(upid: str) -> Optional[str]:
    """Extract the originating node name from a Proxmox UPID string."""
    match = _UPID_NODE_RE.match((upid or "").strip())
    if not match:
        return None
    node = (match.group(1) or "").strip()
    return node or None


class ConsoleTypeUnavailable(Exception):
    """Raised when a caller explicitly requests a console type this VM
    doesn't actually support (e.g. "serial" for a VM with no serialN:
    socket device configured)."""


class ProxmoxPlugin(ServerPlugin):
    """
    Proxmox plugin for VM power management.
    
    Uses Proxmox VE REST API for VM power control.
    
    This plugin supports POWER_CONTROL only:
    - test_connection: Test Proxmox API connection and authentication
    - get_power_state: Get current VM power state (on/off/unknown)
    - power_on: Power on the VM
    - power_off: Power off the VM (soft or hard)
    - power_reset: Reset/reboot the VM
    """
    
    PLUGIN_NAME = "proxmox"
    PLUGIN_VERSION = "1.0.0"
    SUPPORTED_CATEGORIES = [
        PluginCategory.POWER_CONTROL,
        PluginCategory.VM_PROVISIONING,
    ]
    CAPABILITIES = [
        Capability(
            id="power_control",
            display_name="Power Control",
            description="Power on, off, and reset the VM",
            optional=False,
            ui_pattern=UIPattern.STATE_AND_ACTIONS,
            state_action="get_power_state",
            actions=[
                ActionDef("power_on", "Power On", variant="success"),
                ActionDef("power_off", "Power Off", variant="danger"),
                ActionDef("power_reset", "Reset", variant="warning"),
            ],
        ),
        Capability(
            id="vm_provisioning",
            display_name="VM Provisioning",
            description="Create, clone, and configure virtual machines",
            optional=True,
            ui_pattern=UIPattern.ACTIONS_ONLY,
            actions=[
                ActionDef("create_vm", "Create VM", variant="primary"),
                ActionDef("clone_vm_from_template", "Clone Template", variant="primary"),
            ],
        ),
    ]
    CONFIG_TEMPLATE = {
        "type": "object",
        "properties": {
            "hostname": {
                "type": "string",
                "title": "Proxmox Host",
                "description": "Proxmox VE hostname or IP address",
                "required": True
            },
            "username": {
                "type": "string",
                "title": "Username",
                "description": "Proxmox username (e.g., root@pam or user@pve)",
                "required": True
            },
            "password": {
                "type": "string",
                "title": "Password",
                "description": "Proxmox password",
                "format": "password",
                "required": True
            },
            "port": {
                "type": "integer",
                "title": "Port",
                "description": "Proxmox API port (default: 8006)",
                "default": 8006,
                "required": False
            },
            "node": {
                "type": "string",
                "title": "Node Name",
                "description": "Proxmox node name (e.g., proxmox, pve)",
                "required": True
            },
            "vmid": {
                "type": "integer",
                "title": "VM ID",
                "description": "Virtual machine ID",
                "required": True
            },
            "verify_ssl": {
                "type": "boolean",
                "title": "Verify SSL",
                "description": "Verify SSL certificate (disable only for known self-signed certs)",
                "default": True,
                "required": False
            }
        },
        "required": ["hostname", "username", "password", "node", "vmid"]
    }
    
    def __init__(self, config: Dict):
        """Initialize plugin with config."""
        super().__init__(config)
        
        self.hostname = config.get("hostname")
        self.username = config.get("username")
        self.password = config.get("password")
        self.port = config.get("port", 8006)
        self.node = config.get("node")
        self.vmid = config.get("vmid")
        self.verify_ssl = config.get("verify_ssl", True)
        
        # Build base URL
        self.base_url = f"https://{self.hostname}:{self.port}"
        
        # Proxmox API requires a ticket (CSRF token) for authentication
        self.ticket = None
        self.csrf_token = None

        # Optional cluster-wide relocate callback (see set_relocator), used
        # to recover from a 404 caused by HA/live migration racing this
        # exact request.
        self._relocator: Optional[Callable[[], Awaitable[Optional[str]]]] = None
        self._relocated = False

    def set_relocator(self, relocator: Callable[[], Awaitable[Optional[str]]]) -> None:
        """Register a same-VMID cluster-wide node lookup for 404 recovery.

        ``relocator`` is an async callable ``() -> Optional[str]`` that finds
        the node currently hosting this plugin's ``vmid`` cluster-wide (and
        persists it). Invoked at most once per plugin instance, only when a
        node-scoped request 404s -- i.e. the guest was migrated in the race
        between whoever resolved this plugin's placement and this request.
        """
        self._relocator = relocator

    async def _relocate_and_retry(self) -> bool:
        """Try the registered relocator once; update ``self.node`` on success.

        Returns True (and updates ``self.node``) only if a relocator is set,
        hasn't been used yet on this instance, and finds a *different* node
        than the one we're already using -- callers should retry the request
        exactly once when this returns True.
        """
        if self._relocator is None or self._relocated:
            return False
        self._relocated = True
        try:
            new_node = await self._relocator()
        except Exception:
            logger.debug("[ProxmoxPlugin] relocate lookup failed for VM %s", self.vmid, exc_info=True)
            return False
        if not new_node or new_node == self.node:
            return False
        logger.info(
            "[ProxmoxPlugin] VM %s not on node %s; relocated to %s",
            self.vmid, self.node, new_node,
        )
        self.node = new_node
        return True

    async def _get_with_relocate(self, url_for_node: Callable[[], str], *, timeout: float = 10.0):
        """GET a node-scoped URL; on 404, try one relocate + retry.

        ``url_for_node`` is a zero-arg callable that builds the URL from
        ``self.node`` at call time, since a successful relocate updates
        ``self.node`` before the retry.
        """
        headers = await self._get_headers()
        async with httpx.AsyncClient(verify=self.verify_ssl, timeout=timeout) as client:
            response = await client.get(url_for_node(), headers=headers)
        if response.status_code == 404 and await self._relocate_and_retry():
            async with httpx.AsyncClient(verify=self.verify_ssl, timeout=timeout) as client:
                response = await client.get(url_for_node(), headers=headers)
        return response

    async def _post_with_relocate(
        self,
        url_for_node: Callable[[], str],
        *,
        data: Optional[Dict[str, Any]] = None,
        timeout: float = 15.0,
    ):
        """POST a node-scoped URL; on 404, try one relocate + retry (see
        :meth:`_get_with_relocate`)."""
        headers = await self._get_headers()
        async with httpx.AsyncClient(verify=self.verify_ssl, timeout=timeout) as client:
            response = await client.post(url_for_node(), headers=headers, data=data)
        if response.status_code == 404 and await self._relocate_and_retry():
            async with httpx.AsyncClient(verify=self.verify_ssl, timeout=timeout) as client:
                response = await client.post(url_for_node(), headers=headers, data=data)
        return response

    async def _get_auth_ticket(self) -> Dict[str, str]:
        """
        Authenticate with Proxmox API and get ticket/CSRF token.
        
        Returns:
            Dict with 'ticket' and 'CSRFPreventionToken'
        """
        url = f"{self.base_url}/api2/json/access/ticket"
        
        try:
            async with httpx.AsyncClient(verify=self.verify_ssl, timeout=10.0) as client:
                response = await client.post(
                    url,
                    data={
                        "username": self.username,
                        "password": self.password
                    }
                )
                response.raise_for_status()
                
                data = response.json()
                if data.get("data"):
                    ticket_data = data["data"]
                    return {
                        "ticket": ticket_data.get("ticket"),
                        "CSRFPreventionToken": ticket_data.get("CSRFPreventionToken")
                    }
                else:
                    raise Exception("No authentication data returned")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                raise Exception("Authentication failed: Invalid username or password")
            raise Exception(f"HTTP error: {e.response.status_code}")
        except Exception as e:
            raise Exception(f"Failed to authenticate: {str(e)}")
    
    async def _get_headers(self) -> Dict[str, str]:
        """Get HTTP headers with authentication."""
        if not self.ticket:
            auth_data = await self._get_auth_ticket()
            self.ticket = auth_data["ticket"]
            self.csrf_token = auth_data["CSRFPreventionToken"]
        
        return {
            "Cookie": f"PVEAuthCookie={self.ticket}",
            "CSRFPreventionToken": self.csrf_token
        }
    
    async def test_connection(self) -> Dict[str, Any]:
        """
        Test the Proxmox API connection and verify credentials.
        
        Returns:
            Dict with success status, message, and details
        """
        try:
            logger.info(f"[ProxmoxPlugin.test_connection] Testing Proxmox connection to {self.hostname}:{self.port}")
            
            # Authenticate
            auth_data = await self._get_auth_ticket()
            self.ticket = auth_data["ticket"]
            self.csrf_token = auth_data["CSRFPreventionToken"]
            
            # Test by getting VM status
            url = f"{self.base_url}/api2/json/nodes/{self.node}/qemu/{self.vmid}/status/current"
            headers = await self._get_headers()
            
            async with httpx.AsyncClient(verify=self.verify_ssl, timeout=10.0) as client:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                
                vm_data = response.json().get("data", {})
                power_state = vm_data.get("status", "unknown")
                
                logger.info(f"[ProxmoxPlugin.test_connection] Connection test successful for VM {self.vmid}")
                return {
                    "success": True,
                    "message": f"Successfully connected and authenticated to Proxmox API",
                    "details": {
                        "hostname": self.hostname,
                        "port": self.port,
                        "node": self.node,
                        "vmid": self.vmid,
                        "power_state": power_state,
                        "vm_name": vm_data.get("name", "Unknown"),
                        "cpu": vm_data.get("cpu", 0),
                        "mem": vm_data.get("mem", 0),
                        "maxmem": vm_data.get("maxmem", 0)
                    }
                }
                
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                return {
                    "success": False,
                    "message": "Authentication failed: Invalid username or password",
                    "details": {
                        "hostname": self.hostname,
                        "port": self.port,
                        "error": "401 Unauthorized"
                    }
                }
            elif e.response.status_code == 404:
                return {
                    "success": False,
                    "message": f"VM {self.vmid} not found on node {self.node}",
                    "details": {
                        "hostname": self.hostname,
                        "node": self.node,
                        "vmid": self.vmid,
                        "error": "404 Not Found"
                    }
                }
            else:
                return {
                    "success": False,
                    "message": f"HTTP error: {e.response.status_code}",
                    "details": {
                        "hostname": self.hostname,
                        "port": self.port,
                        "error": str(e)
                    }
                }
        except Exception as e:
            error_str = str(e)
            logger.error(f"[ProxmoxPlugin.test_connection] Connection test failed: {error_str}")
            
            if "timeout" in error_str.lower() or "connect" in error_str.lower():
                return {
                    "success": False,
                    "message": f"Connection failed: Could not reach {self.hostname}:{self.port}",
                    "details": {
                        "hostname": self.hostname,
                        "port": self.port,
                        "error": error_str
                    }
                }
            else:
                return {
                    "success": False,
                    "message": f"Connection test failed: {error_str}",
                    "details": {
                        "hostname": self.hostname,
                        "port": self.port,
                        "error": error_str
                    }
                }
    
    async def vm_exists(self) -> bool:
        """
        Return True if a QEMU VM with this vmid exists on the node.

        Used for idempotent provisioning so a retry after a mid-flight failure
        does not attempt to clone over an already-created VMID. Also the
        primary cache-probe for placement resolution: a 404 here first tries
        a one-shot cluster-wide relocate (see :meth:`set_relocator`) before
        being reported as "doesn't exist" to the caller.
        """
        response = await self._get_with_relocate(
            lambda: f"{self.base_url}/api2/json/nodes/{self.node}/qemu/{self.vmid}/status/current"
        )
        if response.status_code == 404:
            return False
        response.raise_for_status()
        return True

    async def guest_agent_ready(self) -> bool:
        """Return True if the QEMU guest agent responds to a ping.

        Calls ``POST /nodes/{node}/qemu/{vmid}/agent/ping``. Proxmox returns 200
        only when the agent is installed, running, and reachable; any error
        (agent not up yet, VM stopped, transient API failure) yields False so a
        deployment step can keep waiting.
        """
        try:
            response = await self._post_with_relocate(
                lambda: f"{self.base_url}/api2/json/nodes/{self.node}/qemu/{self.vmid}/agent/ping"
            )
            return response.status_code == 200
        except Exception as exc:
            logger.debug("[ProxmoxPlugin.guest_agent_ready] agent ping failed for VM %s: %s", self.vmid, exc)
            return False

    async def guest_exec(
        self,
        command: list,
        input_data: Optional[str] = None,
        timeout: float = 180.0,
    ) -> Dict[str, Any]:
        """Run a command via QEMU guest agent and wait for completion.

        Uses ``POST .../agent/exec`` then polls ``.../agent/exec-status``.
        ``command`` is argv (e.g. ``["/bin/sh", "-c", "echo hi"]``).
        Returns dict with ``exitcode``, ``out-data``, ``err-data`` (decoded strings).
        """
        if not command:
            raise ValueError("command must be a non-empty list")
        exec_url = f"{self.base_url}/api2/json/nodes/{self.node}/qemu/{self.vmid}/agent/exec"
        status_url = f"{self.base_url}/api2/json/nodes/{self.node}/qemu/{self.vmid}/agent/exec-status"
        headers = await self._get_headers()
        # Build urlencoded body ourselves. Passing a list of tuples as ``data=``
        # to httpx.AsyncClient can trigger a sync multipart encoder path
        # ("Attempted to send a sync request with an AsyncClient instance").
        pairs = [("command", str(part)) for part in command]
        if input_data is not None:
            pairs.append(("input-data", input_data))
        body = urllib.parse.urlencode(pairs, doseq=True).encode("utf-8")
        post_headers = dict(headers)
        post_headers["Content-Type"] = "application/x-www-form-urlencoded"
        async with httpx.AsyncClient(verify=self.verify_ssl, timeout=60.0) as client:
            response = await client.post(exec_url, headers=post_headers, content=body)
            response.raise_for_status()
            pid = (response.json().get("data") or {}).get("pid")
            if pid is None:
                raise RuntimeError(f"guest-exec returned no pid: {response.text}")
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while loop.time() < deadline:
                try:
                    st = await client.get(status_url, headers=headers, params={"pid": int(pid)})
                    st.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    # Long diskutil operations can briefly trip Proxmox's
                    # guest-exec-status timeout; keep polling until our deadline.
                    body = (exc.response.text or "").lower()
                    if exc.response.status_code >= 500 and "timeout" in body:
                        await asyncio.sleep(1.0)
                        continue
                    raise
                data = st.json().get("data") or {}
                if data.get("exited"):
                    return {
                        "exitcode": data.get("exitcode"),
                        "out-data": data.get("out-data") or "",
                        "err-data": data.get("err-data") or "",
                        "pid": int(pid),
                    }
                await asyncio.sleep(0.4)
        raise TimeoutError(f"guest-exec timed out after {timeout}s (pid={pid})")

    async def _macos_password_accepted(self, username: str, password: str) -> bool:
        """Return True if ``dscl -authonly`` accepts the credentials."""
        script = (
            f"dscl /Local/Default -authonly {shlex.quote(username)} {shlex.quote(password)}; "
            f"echo EXIT:$?"
        )
        result = await self.guest_exec(["/bin/sh", "-c", script])
        out = f"{result.get('out-data') or ''}{result.get('err-data') or ''}"
        return "EXIT:0" in out and "eDSAuthFailed" not in out

    async def _windows_set_user_password(self, username: str, password: str) -> bool:
        """Set a local Windows user password via PowerShell ``Set-LocalUser``.

        Returns True on success. Returns False when PowerShell is unavailable
        (non-Windows guest) so callers can try other fallbacks.
        """
        user_ps = (username or "").replace("'", "''")
        pass_ps = (password or "").replace("'", "''")
        script = (
            "$ErrorActionPreference = 'Stop'; "
            f"$secure = ConvertTo-SecureString '{pass_ps}' -AsPlainText -Force; "
            f"Set-LocalUser -Name '{user_ps}' -Password $secure; "
            "Write-Output 'PW_OK'"
        )
        try:
            result = await self.guest_exec(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    script,
                ],
                timeout=60.0,
            )
        except Exception as exc:
            logger.debug("Windows password fallback guest-exec failed: %s", exc)
            return False
        out = f"{result.get('out-data') or ''}{result.get('err-data') or ''}"
        if result.get("exitcode") in (0, None) and "PW_OK" in out:
            logger.info("Windows password set for %r via Set-LocalUser", username)
            return True
        # powershell.exe missing / not Windows
        if "not found" in out.lower() or "cannot find" in out.lower():
            return False
        if result.get("exitcode") not in (0, None):
            logger.debug(
                "Windows Set-LocalUser failed for %r: exit=%s out=%s",
                username,
                result.get("exitcode"),
                out[:240],
            )
            return False
        return False

    async def guest_set_user_password(
        self,
        username: str,
        password: str,
        old_password: Optional[str] = None,
        old_passwords: Optional[Sequence[str]] = None,
    ) -> None:
        """Set a guest OS user password via agent.

        Prefers Proxmox ``agent/set-user-password`` (Linux/Windows qemu-ga).
        Falls back to Windows ``Set-LocalUser``, then macOS ``dscl`` /
        ``sysadminctl`` via guest-exec when that endpoint is unavailable.

        Secure Token macOS users require the current password
        (``old_password`` / ``old_passwords``). Success is verified with
        ``dscl -authonly`` so a silent Secure Token failure cannot look green.
        """
        url = f"{self.base_url}/api2/json/nodes/{self.node}/qemu/{self.vmid}/agent/set-user-password"
        headers = await self._get_headers()
        async with httpx.AsyncClient(verify=self.verify_ssl, timeout=30.0) as client:
            response = await client.post(
                url,
                headers=headers,
                data={"username": username, "password": password},
            )
            if response.status_code == 200:
                return
            # Apple agent / older agents: CommandNotFound → guest-exec fallbacks
            body = (response.text or "").lower()
            if response.status_code not in (500, 501, 596) and "not found" not in body and "invalid command" not in body:
                response.raise_for_status()

        if await self._windows_set_user_password(username, password):
            return

        # Already at the desired password (idempotent).
        if await self._macos_password_accepted(username, password):
            return

        candidates: List[Optional[str]] = []
        for value in [old_password, *(old_passwords or [])]:
            if value and value != password and value not in candidates:
                candidates.append(str(value))
        # Last resort: single-arg dscl (works only without Secure Token).
        candidates.append(None)

        user_path = shlex.quote(f"/Users/{username}")
        last_detail = ""
        for old in candidates:
            attempts = []
            if old is not None:
                attempts.append(
                    "dscl . -passwd "
                    f"{user_path} {shlex.quote(old)} {shlex.quote(password)} 2>&1; echo EXIT:$?"
                )
                attempts.append(
                    "sysadminctl -resetPasswordFor "
                    f"{shlex.quote(username)} -newPassword {shlex.quote(password)} "
                    f"-oldPassword {shlex.quote(old)} 2>&1; echo EXIT:$?"
                )
            else:
                attempts.append(
                    f"dscl . -passwd {user_path} {shlex.quote(password)} 2>&1; echo EXIT:$?"
                )
            for script in attempts:
                result = await self.guest_exec(["/bin/sh", "-c", script])
                detail = (
                    f"old={'set' if old is not None else 'none'} "
                    f"exit={result.get('exitcode')} "
                    f"out={(result.get('out-data') or '')[:240]!r} "
                    f"err={(result.get('err-data') or '')[:240]!r}"
                )
                last_detail = detail
                if await self._macos_password_accepted(username, password):
                    logger.info("macOS password set for %r (%s)", username, detail)
                    return

        raise RuntimeError(
            f"Failed to set password for {username!r}: agent/set-user-password "
            f"unavailable, Windows Set-LocalUser failed or not applicable, and "
            f"macOS dscl -authonly still rejects the new password. Secure Token "
            f"guests need the current/template password. Last attempt: {last_detail}"
        )

    async def update_smbios1(self, smbios1: str) -> None:
        """Set Proxmox ``smbios1`` config string on this VM."""
        url = f"{self.base_url}/api2/json/nodes/{self.node}/qemu/{self.vmid}/config"
        headers = await self._get_headers()
        async with httpx.AsyncClient(verify=self.verify_ssl, timeout=30.0) as client:
            response = await client.put(url, headers=headers, data={"smbios1": smbios1})
            response.raise_for_status()

    async def update_description(self, description: str) -> None:
        """Set Proxmox qemu ``description`` (Notes) for this VM."""
        url = f"{self.base_url}/api2/json/nodes/{self.node}/qemu/{self.vmid}/config"
        headers = await self._get_headers()
        async with httpx.AsyncClient(verify=self.verify_ssl, timeout=30.0) as client:
            response = await client.put(
                url, headers=headers, data={"description": description or ""}
            )
            response.raise_for_status()

    async def extract_backup_config(self, volume: str) -> str:
        """Return qemu-server.conf from a vzdump/PBS volume (``pvesm extractconfig``)."""
        vol = (volume or "").strip()
        if not vol:
            raise ValueError("volume is required")
        url = f"{self.base_url}/api2/json/nodes/{self.node}/vzdump/extractconfig"
        headers = await self._get_headers()
        async with httpx.AsyncClient(verify=self.verify_ssl, timeout=120.0) as client:
            response = await client.get(url, headers=headers, params={"volume": vol})
            response.raise_for_status()
            data = response.json().get("data")
        if data is None:
            return ""
        return str(data)

    async def guest_shutdown(self) -> None:
        """Ask the guest agent to shut down the guest OS."""
        url = f"{self.base_url}/api2/json/nodes/{self.node}/qemu/{self.vmid}/agent/shutdown"
        headers = await self._get_headers()
        async with httpx.AsyncClient(verify=self.verify_ssl, timeout=30.0) as client:
            response = await client.post(url, headers=headers)
            response.raise_for_status()

    async def get_power_state(self) -> PowerState:
        """
        Get current VM power state from Proxmox API.
        
        Returns:
            PowerState enum value (on/off/unknown)
        """
        try:
            response = await self._get_with_relocate(
                lambda: f"{self.base_url}/api2/json/nodes/{self.node}/qemu/{self.vmid}/status/current"
            )
            response.raise_for_status()

            vm_data = response.json().get("data", {})
            status = vm_data.get("status", "unknown").lower()

            if status == "running":
                return PowerState.ON
            elif status == "stopped":
                return PowerState.OFF
            else:
                return PowerState.UNKNOWN
                    
        except Exception as e:
            logger.error(f"[ProxmoxPlugin.get_power_state] Failed to get power state: {str(e)}")
            return PowerState.UNKNOWN
    
    async def power_on(self) -> bool:
        """
        Power on the VM via Proxmox API.

        Waits for the async start task (UPID) and confirms the guest is running.
        Accepting the HTTP ``/status/start`` alone is not enough — QEMU can still
        fail immediately after (e.g. missing bridge) while the POST returns 200.
        
        Returns:
            True if successful, False otherwise
        """
        try:
            # Already running: treat as success (idempotent for deployment retries).
            if await self.get_power_state() == PowerState.ON:
                logger.info("[ProxmoxPlugin.power_on] VM %s already running", self.vmid)
                return True

            response = await self._post_with_relocate(
                lambda: f"{self.base_url}/api2/json/nodes/{self.node}/qemu/{self.vmid}/status/start",
                timeout=30.0,
            )
            response.raise_for_status()
            upid = response.json().get("data")
            if upid:
                await self.wait_for_proxmox_task(str(upid), timeout=120.0)

            # Confirm QEMU actually reached running (task OK can still race).
            deadline = asyncio.get_running_loop().time() + 30.0
            while asyncio.get_running_loop().time() < deadline:
                if await self.get_power_state() == PowerState.ON:
                    logger.info("[ProxmoxPlugin.power_on] VM %s is running", self.vmid)
                    return True
                await asyncio.sleep(1.0)

            raise RuntimeError(
                f"Proxmox start task finished but VM {self.vmid} is not running"
            )

        except Exception as e:
            logger.error(f"[ProxmoxPlugin.power_on] Failed to power on VM: {str(e)}")
            # Preserve bool API for admin/client power buttons; callers that need the
            # message (deployment steps) should catch via get_power_state / logs.
            # Re-raise task/verify failures so deployment does not mark success silently.
            if isinstance(e, (RuntimeError, TimeoutError, httpx.HTTPError)):
                raise
            return False
    
    async def power_off(self, force: bool = False) -> bool:
        """
        Power off the VM via Proxmox API.
        
        Args:
            force: If True, force shutdown (equivalent to pulling power)
        
        Returns:
            True if successful, False otherwise
        """
        try:
            # Use shutdown for graceful, stop for force
            action = "stop" if force else "shutdown"
            response = await self._post_with_relocate(
                lambda: f"{self.base_url}/api2/json/nodes/{self.node}/qemu/{self.vmid}/status/{action}",
                timeout=30.0,
            )
            response.raise_for_status()

            logger.info(f"[ProxmoxPlugin.power_off] Successfully sent power off command for VM {self.vmid} (force={force})")
            return True
                
        except Exception as e:
            logger.error(f"[ProxmoxPlugin.power_off] Failed to power off VM: {str(e)}")
            return False
    
    async def power_reset(self) -> bool:
        """
        Reset/reboot the VM via Proxmox API.
        Uses hard reset (like pressing reset button) instead of graceful reboot.
        
        Returns:
            True if successful, False otherwise
        """
        try:
            response = await self._post_with_relocate(
                lambda: f"{self.base_url}/api2/json/nodes/{self.node}/qemu/{self.vmid}/status/reset",
                timeout=30.0,
            )
            response.raise_for_status()

            logger.info(f"[ProxmoxPlugin.power_reset] Successfully sent reset command for VM {self.vmid}")
            return True
                
        except Exception as e:
            logger.error(f"[ProxmoxPlugin.power_reset] Failed to reset VM: {str(e)}")
            return False

    async def create_vnc_proxy(self) -> Dict[str, Any]:
        """
        Request a Proxmox VNC console proxy for this VM.

        Calls ``POST /nodes/{node}/qemu/{vmid}/vncproxy`` (with
        ``websocket=1`` so Proxmox permits the follow-up ``vncwebsocket``
        upgrade). The returned ``ticket`` serves double duty in the Proxmox
        VNC model: it is both the query-string credential the
        ``vncwebsocket`` HTTP upgrade requires, and the VNC (RFB) password
        the far end's QEMU VNC server expects during the RFB handshake --
        distinct from (and unrelated to) the Proxmox account credentials on
        this plugin, which are never exposed to a browser.

        Returns:
            Dict with ``port`` (int), ``ticket`` (str VNC/RFB password),
            ``upid``, and ``cert`` (server certificate fingerprint, if any).
        """
        response = await self._post_with_relocate(
            lambda: f"{self.base_url}/api2/json/nodes/{self.node}/qemu/{self.vmid}/vncproxy",
            data={"websocket": 1},
            timeout=15.0,
        )
        response.raise_for_status()
        data = response.json().get("data") or {}
        if not data.get("port") or not data.get("ticket"):
            raise Exception("Proxmox did not return a VNC proxy port/ticket")
        return {
            "port": int(data["port"]),
            "ticket": str(data["ticket"]),
            "upid": data.get("upid"),
            "cert": data.get("cert"),
        }

    async def get_available_console_types(self) -> Dict[str, bool]:
        """
        Return which Proxmox console types are actually usable for this VM,
        as ``{"vnc": bool, "serial": bool}``.

        Mirrors the same distinction Proxmox's own web UI makes when
        deciding whether to grey out its "xterm.js console" option:

        - ``serial`` needs a ``serialN: socket`` hardware device configured
          (``qm set <vmid> -serial0 socket``) -- without one, ``termproxy``
          has nothing to attach to. This is independent of the ``vga``
          setting.
        - ``vnc`` is unavailable only when ``vga`` is itself set to
          ``serialN`` (redirecting the primary display to a serial port
          leaves no framebuffer for noVNC to show -- just a black screen).
          Otherwise noVNC always works, which is why Proxmox treats it as
          the safe fallback whenever the serial console can't.

        Checked via ``GET .../config`` rather than assumed, since it varies
        per VM.
        """
        response = await self._get_with_relocate(
            lambda: f"{self.base_url}/api2/json/nodes/{self.node}/qemu/{self.vmid}/config",
            timeout=15.0,
        )
        response.raise_for_status()
        data = response.json().get("data") or {}

        vga = str(data.get("vga") or "").strip().lower()
        serial_available = any(
            _SERIAL_DEVICE_KEY_RE.match(str(key))
            and str(value).strip().lower().startswith("socket")
            for key, value in data.items()
        )
        vnc_available = not vga.startswith("serial")
        return {"vnc": vnc_available, "serial": serial_available}

    async def create_term_proxy(self) -> Dict[str, Any]:
        """
        Request a Proxmox serial terminal proxy for this VM (the counterpart
        to :meth:`create_vnc_proxy` for VMs configured with ``vga: serialN``).

        Calls ``POST /nodes/{node}/qemu/{vmid}/termproxy``. The returned
        ``port``/``ticket`` are consumed the same way as a VNC proxy's --
        against the same ``vncwebsocket`` endpoint (see
        :meth:`vnc_websocket_url`) -- but the byte stream carries Proxmox's
        xterm.js line-protocol (see ``app.api.vm_vnc``) instead of RFB.
        """
        response = await self._post_with_relocate(
            lambda: f"{self.base_url}/api2/json/nodes/{self.node}/qemu/{self.vmid}/termproxy",
            timeout=15.0,
        )
        response.raise_for_status()
        data = response.json().get("data") or {}
        if not data.get("port") or not data.get("ticket"):
            raise Exception("Proxmox did not return a terminal proxy port/ticket")
        return {
            "port": int(data["port"]),
            "ticket": str(data["ticket"]),
            "upid": data.get("upid"),
            "user": data.get("user") or self.username,
        }

    async def open_console_proxy(self, console_type: Optional[str] = None) -> Dict[str, Any]:
        """
        Open a Proxmox console proxy for this VM and return it in a shape
        callers can handle uniformly.

        Args:
            console_type: ``"vnc"`` or ``"serial"`` to open that type
                specifically (e.g. the user picked one in the UI) -- raises
                :class:`ConsoleTypeUnavailable` if this VM doesn't actually
                support it. If omitted, picks the best default: ``"vnc"``
                when available (Proxmox's own console selector treats it as
                the safe fallback), else ``"serial"``.

        Returns:
            Dict with ``port``, ``ticket``, ``console_type`` (the type that
            was actually opened), and ``available_console_types`` (the
            ``{"vnc": bool, "serial": bool}`` this VM supports, so callers
            don't need a second round-trip to learn it).
        """
        available = await self.get_available_console_types()
        if console_type is not None:
            if console_type not in ("vnc", "serial"):
                raise ValueError(f"Unknown console type: {console_type!r}")
            if not available.get(console_type):
                raise ConsoleTypeUnavailable(f"{console_type} console is not available for this VM")
            chosen = console_type
        else:
            chosen = "vnc" if available.get("vnc") else "serial"

        proxy = await self.create_term_proxy() if chosen == "serial" else await self.create_vnc_proxy()
        return {
            "port": proxy["port"],
            "ticket": proxy["ticket"],
            "console_type": chosen,
            "available_console_types": available,
        }

    def vnc_websocket_url(self, port: int, vnc_ticket: str) -> str:
        """Build the ``wss://`` URL for Proxmox's ``vncwebsocket`` endpoint."""
        query = urllib.parse.urlencode({"port": port, "vncticket": vnc_ticket})
        return (
            f"wss://{self.hostname}:{self.port}/api2/json/nodes/{self.node}"
            f"/qemu/{self.vmid}/vncwebsocket?{query}"
        )

    async def get_vnc_auth_cookie(self) -> str:
        """
        Ensure we're authenticated and return the ``Cookie`` header value the
        ``vncwebsocket`` upgrade request must present (the same
        ``PVEAuthCookie`` used for normal API calls). Separate from -- and
        not to be confused with -- the per-session VNC/RFB password returned
        by :meth:`create_vnc_proxy`.
        """
        await self._get_headers()
        return f"PVEAuthCookie={self.ticket}"
    
    # Unsupported capabilities
    async def create_user(self, username: str, password: str, privileges: list) -> bool:
        raise NotImplementedError("Proxmox plugin does not support user account control")
    
    async def delete_user(self, username: str) -> bool:
        raise NotImplementedError("Proxmox plugin does not support user account control")
    
    async def update_user_password(self, username: str, new_password: str) -> bool:
        raise NotImplementedError("Proxmox plugin does not support user account control")
    
    async def list_users(self) -> list:
        raise NotImplementedError("Proxmox plugin does not support user account control")
    
    async def get_boot_options(self) -> Dict[str, Any]:
        raise NotImplementedError("Proxmox plugin does not support boot order control")

    async def set_boot_order(self, boot_order: list) -> bool:
        raise NotImplementedError("Proxmox plugin does not support boot order control")
    
    async def get_boot_order(self) -> Dict[str, Any]:
        raise NotImplementedError("Proxmox plugin does not support boot order control")
    
    async def set_next_boot_device(
        self,
        device: str,
        persistent: bool = False,
        uefi: Optional[bool] = None,
    ) -> bool:
        raise NotImplementedError("Proxmox plugin does not support boot order control")

    async def create_vm(self, vm_config: Dict[str, Any]) -> Dict[str, Any]:
        vmid = vm_config.get("vmid")
        if vmid is None:
            raise ValueError("vmid is required")
        url = f"{self.base_url}/api2/json/nodes/{self.node}/qemu"
        headers = await self._get_headers()
        payload = {
            "vmid": vmid,
            "name": vm_config.get("name", f"vm-{vmid}"),
            "memory": vm_config.get("memory_mb", 1024),
            "cores": vm_config.get("cores", 1),
        }
        async with httpx.AsyncClient(verify=self.verify_ssl, timeout=30.0) as client:
            response = await client.post(url, headers=headers, data=payload)
            response.raise_for_status()
            return {"vmid": vmid, "task": response.json().get("data")}

    async def clone_vm_from_template(self, template_ref: Dict[str, Any], vm_config: Dict[str, Any]) -> Dict[str, Any]:
        template_vmid = template_ref.get("vmid")
        target_vmid = vm_config.get("vmid")
        if template_vmid is None or target_vmid is None:
            raise ValueError("template vmid and target vmid are required")
        # Optional template home node (shared storage: template may live elsewhere).
        source_node = (template_ref.get("node") or self.node or "").strip() or self.node
        url = f"{self.base_url}/api2/json/nodes/{source_node}/qemu/{template_vmid}/clone"
        headers = await self._get_headers()
        payload = {
            "newid": target_vmid,
            "name": vm_config.get("name", f"vm-{target_vmid}"),
            "full": int(bool(vm_config.get("full_clone", False))),
        }
        if vm_config.get("target_node"):
            payload["target"] = vm_config["target_node"]
        async with httpx.AsyncClient(verify=self.verify_ssl, timeout=30.0) as client:
            response = await client.post(url, headers=headers, data=payload)
            response.raise_for_status()
            return {"vmid": target_vmid, "task": response.json().get("data")}

    async def configure_vm(self, vm_ref: Dict[str, Any], vm_config: Dict[str, Any]) -> bool:
        vmid = vm_ref.get("vmid") or self.vmid
        if vmid is None:
            raise ValueError("vmid is required")
        url = f"{self.base_url}/api2/json/nodes/{self.node}/qemu/{vmid}/config"
        headers = await self._get_headers()
        payload = {}
        if "memory_mb" in vm_config:
            payload["memory"] = vm_config["memory_mb"]
        if "cores" in vm_config:
            payload["cores"] = vm_config["cores"]
        if "sockets" in vm_config:
            payload["sockets"] = vm_config["sockets"]
        # Cloud-init / QEMU network (Proxmox native cloud-init drive)
        if vm_config.get("ipconfig0"):
            payload["ipconfig0"] = str(vm_config["ipconfig0"])
        if vm_config.get("nameserver"):
            payload["nameserver"] = str(vm_config["nameserver"])
        if vm_config.get("ciuser"):
            payload["ciuser"] = str(vm_config["ciuser"])
        if vm_config.get("cipassword"):
            payload["cipassword"] = str(vm_config["cipassword"])
        if vm_config.get("sshkeys"):
            # Already percent-encoded by proxmox_sshkeys_param / callers.
            payload["sshkeys"] = str(vm_config["sshkeys"])
        if not payload:
            return True
        async with httpx.AsyncClient(verify=self.verify_ssl, timeout=30.0) as client:
            response = await client.put(url, headers=headers, data=payload)
            response.raise_for_status()
            return True

    async def regenerate_cloudinit(self, vmid: Optional[int] = None) -> None:
        """Mark the cloud-init drive for rebuild from current QEMU config.

        Proxmox regenerates the NoCloud ISO when the VM (re)starts after
        cloud-init settings change. Re-applying the current ``ipconfig0`` /
        ``nameserver`` / ``ciuser`` keys ensures the drive is dirty even if
        only a soft re-apply is needed. Callers must reboot (and typically
        ``cloud-init clean``) for the guest to consume the new seed.
        """
        target = vmid if vmid is not None else self.vmid
        if target is None:
            raise ValueError("vmid is required")
        cfg = await self.get_qemu_config(int(target))
        payload: Dict[str, Any] = {}
        for key in ("ipconfig0", "nameserver", "ciuser", "cipassword", "searchdomain", "sshkeys"):
            if cfg.get(key) is not None and str(cfg.get(key)) != "":
                payload[key] = str(cfg[key])
        if not payload:
            return
        url = f"{self.base_url}/api2/json/nodes/{self.node}/qemu/{int(target)}/config"
        headers = await self._get_headers()
        async with httpx.AsyncClient(verify=self.verify_ssl, timeout=30.0) as client:
            response = await client.put(url, headers=headers, data=payload)
            response.raise_for_status()

    async def get_qemu_config(self, vmid: Optional[int] = None) -> Dict[str, Any]:
        """Return the Proxmox QEMU config dict for ``vmid`` (or this plugin's VM)."""
        target = vmid if vmid is not None else self.vmid
        if target is None:
            raise ValueError("vmid is required")
        url = f"{self.base_url}/api2/json/nodes/{self.node}/qemu/{target}/config"
        headers = await self._get_headers()
        async with httpx.AsyncClient(verify=self.verify_ssl, timeout=30.0) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            return response.json().get("data") or {}

    async def ensure_network_bridge(
        self,
        bridge: str,
        *,
        vmid: Optional[int] = None,
        net_key: str = "net0",
    ) -> Dict[str, Any]:
        """Set ``bridge=<bridge>`` on a QEMU nic (default ``net0``), preserving model/MAC/opts.

        Linked clones keep the template's bridge; catalog/IP-pool placement must
        rewrite it before power-on when the target node uses a different vmbr.
        """
        target = vmid if vmid is not None else self.vmid
        if target is None:
            raise ValueError("vmid is required")
        bridge_name = (bridge or "").strip()
        if not bridge_name:
            raise ValueError("bridge is required")
        if not re.match(r"^[A-Za-z0-9._-]+$", bridge_name):
            raise ValueError(f"Invalid bridge name: {bridge_name!r}")
        key = (net_key or "net0").strip() or "net0"
        if not re.match(r"^net\d+$", key):
            raise ValueError(f"Invalid net key: {key!r}")

        cfg = await self.get_qemu_config(vmid=int(target))
        current = str(cfg.get(key) or "").strip()
        if not current:
            raise RuntimeError(f"VM {target} has no {key} to retarget onto bridge {bridge_name}")

        # No-op when the target bridge is already present (option order may vary).
        for part in current.split(","):
            if part == f"bridge={bridge_name}":
                return {"changed": False, "net_key": key, "value": current}

        parts = [p for p in current.split(",") if p and not p.startswith("bridge=")]
        parts.append(f"bridge={bridge_name}")
        updated = ",".join(parts)
        if updated == current:
            return {"changed": False, "net_key": key, "value": current}

        url = f"{self.base_url}/api2/json/nodes/{self.node}/qemu/{int(target)}/config"
        headers = await self._get_headers()
        async with httpx.AsyncClient(verify=self.verify_ssl, timeout=30.0) as client:
            response = await client.put(url, headers=headers, data={key: updated})
            response.raise_for_status()
        return {"changed": True, "net_key": key, "value": updated, "previous": current}

    async def resize_disk(self, disk: str, size: str, vmid: Optional[int] = None) -> None:
        """
        Resize a VM disk via Proxmox ``PUT .../resize``.

        ``size`` is a Proxmox size string (absolute ``100G`` or relative ``+20G``).
        """
        target = vmid if vmid is not None else self.vmid
        if target is None:
            raise ValueError("vmid is required")
        if not disk or not size:
            raise ValueError("disk and size are required")
        url = f"{self.base_url}/api2/json/nodes/{self.node}/qemu/{target}/resize"
        headers = await self._get_headers()
        async with httpx.AsyncClient(verify=self.verify_ssl, timeout=120.0) as client:
            response = await client.put(url, headers=headers, data={"disk": disk, "size": size})
            response.raise_for_status()
            upid = response.json().get("data")
        if upid:
            await self.wait_for_proxmox_task(str(upid), timeout=600.0)

    async def ensure_primary_disk_gb(self, target_gb: int, vmid: Optional[int] = None) -> Dict[str, Any]:
        """
        Grow the largest hard disk to at least ``target_gb`` GiB if needed.

        Never shrinks. Returns a small status dict for deployment logs.
        """
        from app.services.deployment.disk_sizing import needs_grow, select_primary_disk

        target = int(target_gb)
        cfg = await self.get_qemu_config(vmid)
        selected = select_primary_disk(cfg)
        if not selected:
            raise RuntimeError("No resizable primary disk found in QEMU config")
        disk_key, current_gb = selected
        if not needs_grow(current_gb, target):
            return {
                "resized": False,
                "disk": disk_key,
                "current_gb": current_gb,
                "target_gb": target,
            }
        await self.resize_disk(disk_key, f"{target}G", vmid=vmid)
        return {
            "resized": True,
            "disk": disk_key,
            "current_gb": current_gb,
            "target_gb": target,
        }

    async def get_proxmox_task_status(self, upid: str) -> Dict[str, Any]:
        """Return one poll of ``/nodes/{node}/tasks/{upid}/status`` (empty if no upid).

        Cross-node clones (template on A, target on B) return a UPID whose node
        is the template/home node. Always poll that node from the UPID, not the
        plugin's placed ``self.node``.
        """
        if not upid:
            return {}
        task_node = proxmox_node_from_upid(upid) or self.node
        encoded = urllib.parse.quote(upid, safe="")
        url = f"{self.base_url}/api2/json/nodes/{task_node}/tasks/{encoded}/status"
        headers = await self._get_headers()
        async with httpx.AsyncClient(verify=self.verify_ssl, timeout=60.0) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            data = response.json().get("data") or {}
        return data if isinstance(data, dict) else {}

    async def wait_for_proxmox_task(self, upid: str, timeout: float = 300.0, interval: float = 1.5) -> None:
        """Poll task status until finished or timeout (node taken from UPID)."""
        if not upid:
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            data = await self.get_proxmox_task_status(upid)
            status = (data.get("status") or "").lower()
            if status == "stopped":
                exitstatus = (data.get("exitstatus") or "").upper()
                if exitstatus == "OK":
                    return
                raise RuntimeError(f"Proxmox task failed ({upid}): {data!r}")
            await asyncio.sleep(interval)
        raise TimeoutError(f"Proxmox task timed out after {timeout}s: {upid}")

    async def delete_vm(self, vm_ref: Dict[str, Any]) -> bool:
        vmid = vm_ref.get("vmid") or self.vmid
        if vmid is None:
            raise ValueError("vmid is required")
        url = f"{self.base_url}/api2/json/nodes/{self.node}/qemu/{vmid}"
        headers = await self._get_headers()
        async with httpx.AsyncClient(verify=self.verify_ssl, timeout=30.0) as client:
            response = await client.delete(url, headers=headers)
            response.raise_for_status()
            return True

    async def get_next_vmid(self) -> int:
        """Ask Proxmox for the next free VMID (respects unique-next-id / used_vmids)."""
        url = f"{self.base_url}/api2/json/cluster/nextid"
        headers = await self._get_headers()
        async with httpx.AsyncClient(verify=self.verify_ssl, timeout=15.0) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            data = response.json().get("data")
        if data is None:
            raise RuntimeError("Proxmox nextid returned no data")
        return int(data)

    async def check_vmid_available_for_new(self, vmid: int) -> bool:
        """
        Return True if Proxmox would accept ``vmid`` as a *new* allocation.

        Uses ``GET /cluster/nextid?vmid=…``. With unique-next-id enabled this
        rejects IDs already recorded in used_vmids.list. Sticky recreate of an
        already-reserved service VMID must not use this check.
        """
        url = f"{self.base_url}/api2/json/cluster/nextid"
        headers = await self._get_headers()
        async with httpx.AsyncClient(verify=self.verify_ssl, timeout=15.0) as client:
            response = await client.get(url, headers=headers, params={"vmid": int(vmid)})
            if response.status_code == 200:
                data = response.json().get("data")
                try:
                    return int(data) == int(vmid)
                except (TypeError, ValueError):
                    return False
            return False

    async def list_backups(self, storage: str, vmid: Optional[int] = None) -> List[Dict[str, Any]]:
        """List backup content on a storage, optionally filtered to a VMID."""
        if not storage:
            raise ValueError("storage is required")
        encoded_storage = urllib.parse.quote(str(storage), safe="")
        url = f"{self.base_url}/api2/json/nodes/{self.node}/storage/{encoded_storage}/content"
        headers = await self._get_headers()
        params: Dict[str, Any] = {"content": "backup"}
        if vmid is not None:
            params["vmid"] = int(vmid)
        async with httpx.AsyncClient(verify=self.verify_ssl, timeout=60.0) as client:
            response = await client.get(url, headers=headers, params=params)
            response.raise_for_status()
            rows = response.json().get("data") or []
        out: List[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_vmid = row.get("vmid")
            if vmid is not None and row_vmid is not None and int(row_vmid) != int(vmid):
                continue
            volid = row.get("volid") or row.get("volume")
            out.append(
                {
                    "volid": volid,
                    "storage": storage,
                    "vmid": int(row_vmid) if row_vmid is not None else vmid,
                    "ctime": row.get("ctime"),
                    "size": row.get("size"),
                    "format": row.get("format"),
                    "notes": row.get("notes") or row.get("comment"),
                    "subtype": row.get("subtype"),
                }
            )
        return out

    @staticmethod
    def _notes_template(notes: str) -> str:
        """Escape a free-text name/notes string for vzdump ``notes-template``.

        PVE applies ``{{var}}`` substitution and requires single-line templates
        with escaped backslashes/newlines.
        """
        text = (notes or "").strip()
        if not text:
            return ""
        text = text.replace("\\", "\\\\").replace("\r", "").replace("\n", "\\n")
        # Prevent accidental template expansion of user input.
        text = text.replace("{{", "{ {").replace("}}", "} }")
        return text[:1024]

    async def create_backup(
        self,
        vmid: Optional[int] = None,
        *,
        storage: str,
        notes: Optional[str] = None,
        mode: str = "snapshot",
        compress: Optional[str] = None,
    ) -> str:
        """Start a vzdump backup to ``storage``. Returns the Proxmox UPID.

        Free-text ``notes`` are sent as vzdump ``notes-template`` (applied when
        the backup finishes). ``compress`` is omitted for PBS unless set.
        """
        target = int(vmid if vmid is not None else self.vmid)
        if not storage:
            raise ValueError("storage is required")
        url = f"{self.base_url}/api2/json/nodes/{self.node}/vzdump"
        headers = await self._get_headers()
        payload: Dict[str, Any] = {
            "vmid": target,
            "storage": storage,
            "mode": mode or "snapshot",
        }
        template = self._notes_template(notes or "")
        if template:
            payload["notes-template"] = template
        if compress:
            payload["compress"] = compress
        async with httpx.AsyncClient(verify=self.verify_ssl, timeout=60.0) as client:
            response = await client.post(url, headers=headers, data=payload)
            response.raise_for_status()
            upid = response.json().get("data")
        if not upid:
            raise RuntimeError("Proxmox vzdump returned no task UPID")
        return str(upid)

    async def update_backup_notes(self, storage: str, volume: str, notes: str) -> None:
        """Set notes on an existing backup volume (PBS/PVE storage content API)."""
        url = self._backup_content_url(self.base_url, self.node, storage, volume)
        headers = await self._get_headers()
        async with httpx.AsyncClient(verify=self.verify_ssl, timeout=60.0) as client:
            response = await client.put(url, headers=headers, data={"notes": notes or ""})
            response.raise_for_status()

    async def list_tasks(
        self,
        *,
        running_only: bool = True,
        vmid: Optional[int] = None,
        limit: int = 50,
        typefilter: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """List recent/active tasks on this node (optionally filtered by VMID).

        Proxmox uses ``source=active|archive|all`` (not a ``running`` flag).
        """
        url = f"{self.base_url}/api2/json/nodes/{self.node}/tasks"
        headers = await self._get_headers()
        params: Dict[str, Any] = {"limit": int(limit)}
        if running_only:
            params["source"] = "active"
        if vmid is not None:
            params["vmid"] = int(vmid)
        if typefilter:
            params["typefilter"] = typefilter
        async with httpx.AsyncClient(verify=self.verify_ssl, timeout=30.0) as client:
            response = await client.get(url, headers=headers, params=params)
            response.raise_for_status()
            rows = response.json().get("data") or []
        out: List[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            out.append(
                {
                    "upid": row.get("upid"),
                    "type": row.get("type"),
                    "status": row.get("status") or ("RUNNING" if running_only else None),
                    "vmid": row.get("id") or row.get("vmid"),
                    "user": row.get("user"),
                    "starttime": row.get("starttime"),
                    "node": row.get("node") or self.node,
                    "pid": row.get("pid"),
                }
            )
        return out

    @staticmethod
    def _backup_content_url(base_url: str, node: str, storage: str, volume: str) -> str:
        """Build ``.../storage/{storage}/content/{volid}`` for PBS/PVE backups.

        Proxmox validates ``volume`` as a full volid (``storage:backup/...``).
        Relative paths like ``backup/vm/...`` fail parameter verification.
        """
        store = (storage or "").strip()
        vol = str(volume or "").strip()
        if not store or not vol:
            raise ValueError("storage and volume are required")
        # Full volid is "storage:path". PBS snapshot paths also contain ":" in
        # timestamps (…T01:43:19Z), so only split when the left side looks like
        # a storage id (no "/").
        if ":" in vol:
            store_part, _, rest = vol.partition(":")
            if store_part and rest and "/" not in store_part:
                store = store_part
                vol = rest
        volid = f"{store}:{vol}"
        encoded_storage = urllib.parse.quote(store, safe="")
        # Keep ":" and "/" unescaped so PVE can parse the volid path segment.
        encoded_vol = urllib.parse.quote(volid, safe=":/")
        return (
            f"{base_url}/api2/json/nodes/{node}/storage/"
            f"{encoded_storage}/content/{encoded_vol}"
        )

    async def delete_backup(self, storage: str, volume: str) -> None:
        """Delete a backup volume from ``storage`` (``volume`` is volid or relative name)."""
        url = self._backup_content_url(self.base_url, self.node, storage, volume)
        headers = await self._get_headers()
        async with httpx.AsyncClient(verify=self.verify_ssl, timeout=120.0) as client:
            response = await client.delete(url, headers=headers)
            response.raise_for_status()

    async def restore_backup(
        self,
        *,
        archive: str,
        vmid: Optional[int] = None,
        force: bool = True,
        storage: Optional[str] = None,
        start: bool = False,
    ) -> str:
        """
        Restore a backup archive onto ``vmid`` (force-overwrite when ``force``).

        ``archive`` should be a full Proxmox volid (e.g. ``pbs-rf-client:backup/vm/101/…``).
        Returns the Proxmox UPID.
        """
        target = int(vmid if vmid is not None else self.vmid)
        if not archive:
            raise ValueError("archive is required")
        url = f"{self.base_url}/api2/json/nodes/{self.node}/qemu"
        headers = await self._get_headers()
        payload: Dict[str, Any] = {
            "vmid": target,
            "archive": str(archive),
            "force": 1 if force else 0,
            "start": 1 if start else 0,
        }
        if storage:
            payload["storage"] = str(storage)
        async with httpx.AsyncClient(verify=self.verify_ssl, timeout=60.0) as client:
            response = await client.post(url, headers=headers, data=payload)
            response.raise_for_status()
            upid = response.json().get("data")
        if not upid:
            raise RuntimeError("Proxmox restore returned no task UPID")
        return str(upid)
