"""Shared guest-agent configuration helpers (Linux + macOS + Windows)."""
from __future__ import annotations

import asyncio
import logging
import shlex
from typing import Any, Dict, List, Optional

from app.services.deployment.step import DeploymentError
from app.services.macos_smbios import generate_smbios, smbios1_config_value
from app.utils.ipv4_netmask import ipv4_netmask_to_prefixlen

logger = logging.getLogger(__name__)


def _ps_quote(value: str) -> str:
    """Escape a string for use inside a PowerShell single-quoted literal."""
    return (value or "").replace("'", "''")


def _windows_powershell_argv(script: str) -> List[str]:
    return [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        script,
    ]


# Shared adapter discovery used by network + grow helpers.
_WINDOWS_ADAPTER_PS = (
    "$adapter = Get-NetAdapter | Where-Object { "
    "$_.Status -eq 'Up' -and -not $_.Name.StartsWith('Loopback') "
    "} | Select-Object -First 1; "
    "if (-not $adapter) { throw 'No usable network adapter found' }; "
    "$ifIndex = $adapter.ifIndex; "
)


def strategy_options_from_ctx(ctx) -> Dict[str, Any]:
    """Merged strategy options from vm_plan + template defaults."""
    service_cfg = ctx.service.config or {}
    vm_plan = service_cfg.get("vm_plan") or {}
    cfg: Dict[str, Any] = {}
    # Prefer strategy_plan.strategy_config (from VMProvisioningService)
    plan = vm_plan.get("strategy_plan") or {}
    if isinstance(plan.get("strategy_config"), dict):
        cfg.update(plan["strategy_config"])
    # Legacy / snapshot shape
    os_profile = vm_plan.get("os_profile") or {}
    if isinstance(os_profile.get("strategy_config"), dict):
        cfg.update(os_profile["strategy_config"])
    snap = service_cfg.get("product_snapshot") or getattr(ctx.service, "product_snapshot", None) or {}
    if isinstance(snap, dict):
        op = snap.get("os_profile") or {}
        if isinstance(op.get("strategy_config"), dict):
            for k, v in op["strategy_config"].items():
                cfg.setdefault(k, v)
    # effective_specs may carry provision-time password / overrides
    specs = ctx.get_specs()
    for key in (
        "guest_username",
        "network_mode",
        "randomize_smbios",
        "smbios_model",
        "opencore_disk",
        "opencore_config",
        "client_actions",
        "admin_password",
        "guest_password",
        "cloudinit_cipassword",
        "cloudinit_ciuser",
        "template_password",
        "initial_password",
    ):
        if key in specs and specs[key] is not None:
            cfg[key] = specs[key]
    # Desired guest password: prefer template_parameters (Change Password /
    # WHMCS) over stale effective_specs from the original provision enqueue.
    tpl = service_cfg.get("template_parameters") or {}
    if isinstance(tpl, dict):
        if tpl.get("admin_password"):
            cfg["admin_password"] = tpl["admin_password"]
            cfg["guest_password"] = tpl["admin_password"]
        elif tpl.get("guest_password"):
            cfg["guest_password"] = tpl["guest_password"]
    if not cfg.get("guest_password") and not cfg.get("admin_password"):
        if cfg.get("cloudinit_cipassword"):
            cfg["guest_password"] = cfg["cloudinit_cipassword"]
    return cfg


def old_guest_passwords_from_ctx(ctx, *, include_stored: bool = True) -> list:
    """Candidate current passwords for Secure Token ``dscl -passwd`` (old → new).

    ``template_password`` / ``initial_password`` is the clone image default.
    Stored ``template_parameters.admin_password`` is what we last believed was
    on the guest (used for change_password; skip during first provision set).
    """
    opts = strategy_options_from_ctx(ctx)
    out: list = []
    for key in ("template_password", "initial_password"):
        value = opts.get(key)
        if value:
            out.append(str(value))
    if include_stored:
        tpl = (ctx.service.config or {}).get("template_parameters") or {}
        for key in ("previous_admin_password", "admin_password", "guest_password"):
            value = tpl.get(key)
            if value:
                out.append(str(value))
    # Preserve order, drop empties/dupes
    seen = set()
    unique = []
    for value in out:
        if value and value not in seen:
            seen.add(value)
            unique.append(value)
    return unique


async def wait_for_agent(plugin, timeout: float = 600.0, interval: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            if await plugin.guest_agent_ready():
                return
        except Exception:
            pass
        await asyncio.sleep(interval)
    raise DeploymentError("Guest agent did not become ready in time")


async def set_guest_password(
    plugin,
    username: str,
    password: str,
    old_password: str | None = None,
    old_passwords: list | None = None,
) -> None:
    if not username or not password:
        raise DeploymentError("guest username and password are required")
    await plugin.guest_set_user_password(
        username,
        password,
        old_password=old_password,
        old_passwords=old_passwords,
    )


DEFAULT_DNS_PRIMARY = "1.1.1.1"
DEFAULT_DNS_SECONDARY = "8.8.8.8"
DEFAULT_IPV4_NETMASK = "255.255.255.0"
DEFAULT_DNS_SERVER_LIST = (DEFAULT_DNS_PRIMARY, DEFAULT_DNS_SECONDARY)
DEFAULT_CLOUDINIT_NAMESERVERS = f"{DEFAULT_DNS_PRIMARY} {DEFAULT_DNS_SECONDARY}"


def cloudinit_credentials_from_ctx(ctx) -> tuple[Optional[str], Optional[str]]:
    """Resolve ``(ciuser, cipassword)`` for Proxmox cloud-init from service context.

    Username is always ``root`` — Linux cloud-init templates must not create an
    extra OS user; only root's password is set via ``cipassword``.
    """
    opts = strategy_options_from_ctx(ctx)
    specs = ctx.get_specs() if hasattr(ctx, "get_specs") else {}
    tpl = ((getattr(ctx, "service", None) and ctx.service.config) or {}).get("template_parameters") or {}
    if not isinstance(tpl, dict):
        tpl = {}

    password = (
        opts.get("guest_password")
        or opts.get("admin_password")
        or opts.get("cloudinit_cipassword")
        or (specs or {}).get("cloudinit_cipassword")
        or tpl.get("admin_password")
        or tpl.get("guest_password")
    )
    pass_out = str(password) if password else None
    if pass_out == "":
        pass_out = None
    return "root", pass_out


def build_cloudinit_network_payload(
    alloc,
    *,
    mode: str = "static",
    ciuser: Optional[str] = None,
    cipassword: Optional[str] = None,
    nameserver: Optional[str] = None,
    ssh_public_keys: Optional[list] = None,
) -> Dict[str, Any]:
    """Build Proxmox ``configure_vm`` payload for cloud-init network (+ optional creds)."""
    from app.services.ssh_public_keys import proxmox_sshkeys_param

    mode = (mode or "static").lower()
    if mode == "dhcp":
        payload: Dict[str, Any] = {"ipconfig0": "ip=dhcp"}
    else:
        if not alloc or not (alloc.ip_address or "").strip() or not (alloc.gateway or "").strip():
            raise DeploymentError("Static cloud-init network requires ip_address and gateway")
        ip = (alloc.ip_address or "").strip()
        gw = (alloc.gateway or "").strip()
        prefix = ipv4_netmask_to_prefixlen(alloc.subnet_mask)
        payload = {"ipconfig0": f"ip={ip}/{prefix},gw={gw}"}

    ns = (nameserver or DEFAULT_CLOUDINIT_NAMESERVERS).strip()
    if ns:
        payload["nameserver"] = ns
    if ciuser:
        payload["ciuser"] = str(ciuser)
    if cipassword:
        payload["cipassword"] = str(cipassword)
    sshkeys = proxmox_sshkeys_param(ssh_public_keys or [])
    if sshkeys:
        payload["sshkeys"] = sshkeys
    return payload


async def apply_root_authorized_keys(plugin, keys: list) -> None:
    """Replace ``/root/.ssh/authorized_keys`` via guest agent (full replace)."""
    from app.services.ssh_public_keys import format_authorized_keys

    blob = format_authorized_keys(keys)
    # Use printf %b with octal escapes so we never put the raw key material in a
    # way that breaks shell quoting oddly; base64 is clearer.
    import base64

    b64 = base64.b64encode(blob.encode("utf-8")).decode("ascii")
    script = (
        "set -e; "
        "mkdir -p /root/.ssh; chmod 700 /root/.ssh; "
        f"echo {shlex.quote(b64)} | base64 -d > /root/.ssh/authorized_keys; "
        "chmod 600 /root/.ssh/authorized_keys; "
        # Empty blob still creates an empty file (clears keys).
        "true"
    )
    result = await plugin.guest_exec(["/bin/sh", "-c", script])
    if result.get("exitcode") not in (0, None):
        raise DeploymentError(
            f"Failed to write root authorized_keys: exit={result.get('exitcode')} "
            f"err={result.get('err-data')!r}"
        )


async def apply_cloudinit_network_reset(
    plugin,
    alloc,
    *,
    mode: str = "static",
    nameserver: Optional[str] = None,
) -> None:
    """Re-apply IPAM networking via Proxmox cloud-init, then clean + reboot the guest.

    Updating ``ipconfig0`` alone does not re-run cloud-init inside an already
    provisioned guest, so we regenerate the drive, ``cloud-init clean``, and reboot.
    """
    payload = build_cloudinit_network_payload(alloc, mode=mode, nameserver=nameserver)
    vmid = getattr(plugin, "vmid", None)
    ok = await plugin.configure_vm({"vmid": vmid}, payload)
    if not ok:
        raise DeploymentError("Proxmox cloud-init network config update failed")
    if hasattr(plugin, "regenerate_cloudinit"):
        await plugin.regenerate_cloudinit(vmid=vmid)
    try:
        await plugin.guest_exec(
            ["/bin/sh", "-c", "cloud-init clean --logs 2>/dev/null || cloud-init clean || true"]
        )
    except Exception as exc:
        logger.warning("cloud-init clean via guest agent failed (continuing to reboot): %s", exc)
    await reboot_guest_and_wait_agent(plugin, timeout=600.0)


async def configure_linux_network(plugin, *, mode: str, alloc=None, dns: Optional[str] = None) -> None:
    mode = (mode or "static").lower()
    if mode == "dhcp":
        script = (
            "IFACE=$(ip -o link show | awk -F': ' '!/lo/{print $2; exit}'); "
            "command -v nmcli >/dev/null && nmcli dev connect \"$IFACE\" || "
            "dhclient -v \"$IFACE\" || true"
        )
        result = await plugin.guest_exec(["/bin/sh", "-c", script])
        if result.get("exitcode") not in (0, None):
            logger.warning("Linux DHCP configure exit=%s err=%s", result.get("exitcode"), result.get("err-data"))
        return

    if not alloc or not (alloc.ip_address or "").strip():
        raise DeploymentError("Static network requires a VM IP allocation")
    ip = (alloc.ip_address or "").strip()
    prefix = ipv4_netmask_to_prefixlen(alloc.subnet_mask)
    gw = (alloc.gateway or "").strip()
    dns_servers = (
        dns or getattr(alloc, "dns_servers", None) or DEFAULT_CLOUDINIT_NAMESERVERS
    ).replace(",", " ")
    script = f"""
set -e
IFACE=$(ip -o link show | awk -F': ' '!/lo/{{print $2; exit}}')
if command -v nmcli >/dev/null 2>&1; then
  CON=$(nmcli -t -f NAME,DEVICE con show --active | awk -F: -v d="$IFACE" '$2==d{{print $1; exit}}')
  CON=${{CON:-$IFACE}}
  nmcli con mod "$CON" ipv4.method manual ipv4.addresses {shlex.quote(f"{ip}/{prefix}")} ipv4.gateway {shlex.quote(gw)} ipv4.dns {shlex.quote(dns_servers)}
  nmcli con up "$CON" || true
else
  ip addr flush dev "$IFACE" || true
  ip addr add {shlex.quote(f"{ip}/{prefix}")} dev "$IFACE"
  ip link set "$IFACE" up
  ip route replace default via {shlex.quote(gw)} || true
fi
"""
    result = await plugin.guest_exec(["/bin/sh", "-c", script])
    if result.get("exitcode") not in (0, None):
        raise DeploymentError(
            f"Linux static network configure failed: exit={result.get('exitcode')} "
            f"err={result.get('err-data')!r}"
        )


async def _macos_network_service(plugin) -> str:
    result = await plugin.guest_exec(
        ["/usr/sbin/networksetup", "-listallnetworkservices"]
    )
    out = result.get("out-data") or ""
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("An asterisk"):
            continue
        # Prefer Ethernet / USB / first hardware service
        return line
    raise DeploymentError("No macOS network service found via networksetup")


async def configure_macos_network(plugin, *, mode: str, alloc=None, dns: Optional[str] = None) -> None:
    service = await _macos_network_service(plugin)
    svc_q = shlex.quote(service)
    mode = (mode or "static").lower()
    if mode == "dhcp":
        result = await plugin.guest_exec(
            ["/bin/sh", "-c", f"/usr/sbin/networksetup -setdhcp {svc_q}"]
        )
        if result.get("exitcode") not in (0, None):
            raise DeploymentError(f"macOS setdhcp failed: {result.get('err-data')!r}")
        return

    if not alloc or not (alloc.ip_address or "").strip():
        raise DeploymentError("Static network requires a VM IP allocation")
    ip = (alloc.ip_address or "").strip()
    mask = (alloc.subnet_mask or "").strip() or DEFAULT_IPV4_NETMASK
    gw = (alloc.gateway or "").strip()
    script = (
        f"/usr/sbin/networksetup -setmanual {svc_q} "
        f"{shlex.quote(ip)} {shlex.quote(mask)} {shlex.quote(gw)}"
    )
    result = await plugin.guest_exec(["/bin/sh", "-c", script])
    if result.get("exitcode") not in (0, None):
        raise DeploymentError(f"macOS setmanual failed: {result.get('err-data')!r}")
    dns_servers = (dns or DEFAULT_CLOUDINIT_NAMESERVERS).replace(",", " ").split()
    dns_cmd = "/usr/sbin/networksetup -setdnsservers " + svc_q + " " + " ".join(
        shlex.quote(d) for d in dns_servers
    )
    await plugin.guest_exec(["/bin/sh", "-c", dns_cmd])


async def configure_windows_network(plugin, *, mode: str, alloc=None, dns: Optional[str] = None) -> None:
    """Configure the first Up non-loopback adapter via PowerShell guest-exec."""
    mode = (mode or "static").lower()
    if mode == "dhcp":
        script = (
            "$ErrorActionPreference = 'Stop'; "
            + _WINDOWS_ADAPTER_PS
            + "Set-NetIPInterface -InterfaceIndex $ifIndex -Dhcp Enabled; "
            "Set-DnsClientServerAddress -InterfaceIndex $ifIndex -ResetServerAddresses; "
            "try { ipconfig /renew | Out-Null } catch {}; "
            "Write-Output 'NET_OK'"
        )
        result = await plugin.guest_exec(_windows_powershell_argv(script), timeout=120.0)
        out = f"{result.get('out-data') or ''}{result.get('err-data') or ''}"
        if result.get("exitcode") not in (0, None) or "NET_OK" not in out:
            logger.warning(
                "Windows DHCP configure exit=%s err=%s",
                result.get("exitcode"),
                result.get("err-data"),
            )
        return

    if not alloc or not (alloc.ip_address or "").strip():
        raise DeploymentError("Static network requires a VM IP allocation")
    ip = (alloc.ip_address or "").strip()
    prefix = ipv4_netmask_to_prefixlen(alloc.subnet_mask)
    gw = (alloc.gateway or "").strip()
    dns_raw = (
        dns or getattr(alloc, "dns_servers", None) or DEFAULT_CLOUDINIT_NAMESERVERS
    ).replace(",", " ")
    dns_list = [d for d in dns_raw.split() if d]
    if not dns_list:
        dns_list = list(DEFAULT_DNS_SERVER_LIST)
    dns_ps = ",".join("'" + _ps_quote(d) + "'" for d in dns_list)
    gw_line = ""
    if gw:
        gw_line = f"-DefaultGateway '{_ps_quote(gw)}' "
    script = (
        "$ErrorActionPreference = 'Stop'; "
        + _WINDOWS_ADAPTER_PS
        + "Get-NetIPAddress -InterfaceIndex $ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue "
        "| Remove-NetIPAddress -Confirm:$false -ErrorAction SilentlyContinue; "
        "Get-NetRoute -InterfaceIndex $ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue "
        "| Where-Object { $_.DestinationPrefix -eq '0.0.0.0/0' } "
        "| Remove-NetRoute -Confirm:$false -ErrorAction SilentlyContinue; "
        f"New-NetIPAddress -InterfaceIndex $ifIndex -IPAddress '{_ps_quote(ip)}' "
        f"-PrefixLength {int(prefix)} {gw_line}| Out-Null; "
        f"Set-DnsClientServerAddress -InterfaceIndex $ifIndex -ServerAddresses @({dns_ps}); "
        "Write-Output 'NET_OK'"
    )
    result = await plugin.guest_exec(_windows_powershell_argv(script), timeout=120.0)
    out = f"{result.get('out-data') or ''}{result.get('err-data') or ''}"
    if result.get("exitcode") not in (0, None) or "NET_OK" not in out:
        raise DeploymentError(
            f"Windows static network configure failed: exit={result.get('exitcode')} "
            f"err={result.get('err-data')!r} out={result.get('out-data')!r}"
        )


async def discover_opencore_disk(plugin) -> str:
    """Return the diskutil identifier for the OPENCORE EFI volume (e.g. disk1s1)."""
    result = await plugin.guest_exec(["/usr/sbin/diskutil", "list"])
    out = result.get("out-data") or ""
    # Match lines like: 1: EFI OPENCORE 1.1 GB diskXsY
    import re

    for line in out.splitlines():
        if "OPENCORE" in line.upper() or ( "EFI" in line and "OPENCORE" in out.upper()):
            m = re.search(r"\b(disk\d+s\d+)\b", line)
            if m and "OPENCORE" in line.upper():
                return m.group(1)
    # Fallback: mount by volume name
    result = await plugin.guest_exec(
        ["/bin/sh", "-c", "diskutil list | awk '/OPENCORE/{print $NF; exit}'"]
    )
    ident = (result.get("out-data") or "").strip().splitlines()
    if ident and ident[0].startswith("disk"):
        return ident[0].strip()
    raise DeploymentError("Could not find OPENCORE EFI partition via diskutil list")


async def apply_opencore_smbios(
    plugin,
    *,
    model: str = "iMacPro1,1",
    oc_disk: Optional[str] = None,
    cfg_path: str = "/Volumes/OPENCORE/EFI/OC/config.plist",
    sku: Optional[str] = None,
) -> Dict[str, str]:
    """Patch OpenCore PlatformInfo via guest-exec and align Proxmox smbios1.

    Preserves an existing smbios1 ``sku`` (RackFlow rf1 token) unless ``sku``
    is passed explicitly.
    """
    from app.services.vm_identity_stamp import get_smbios1_sku

    sm = generate_smbios(model)
    preserved_sku = sku
    if preserved_sku is None:
        try:
            cfg = await plugin.get_qemu_config()
            preserved_sku = get_smbios1_sku(cfg.get("smbios1") if isinstance(cfg, dict) else None)
        except Exception:
            preserved_sku = None
    # Prefer live discovery — clone/boot order can swap disk numbers vs the template.
    try:
        disk_id = await discover_opencore_disk(plugin)
    except DeploymentError:
        disk_id = (oc_disk or "").strip() or "disk1s1"
    mount = await plugin.guest_exec(["/usr/sbin/diskutil", "mount", disk_id])
    if mount.get("exitcode") not in (0, None):
        logger.info("diskutil mount %s: %s %s", disk_id, mount.get("out-data"), mount.get("err-data"))
    # Resolve config path if volume name differs
    find_cfg = await plugin.guest_exec(
        [
            "/bin/sh",
            "-c",
            "ls /Volumes/OPENCORE/EFI/OC/config.plist 2>/dev/null "
            "|| ls /Volumes/*/EFI/OC/config.plist 2>/dev/null | head -1",
        ]
    )
    resolved = (find_cfg.get("out-data") or "").strip().splitlines()
    cfg = resolved[0].strip() if resolved else cfg_path
    if not cfg:
        raise DeploymentError("OpenCore config.plist not found under /Volumes after mount")
    rom = sm["ROM_HEX"]
    script = f"""
set -e
CFG={shlex.quote(cfg)}
test -f "$CFG"
cp "$CFG" "$CFG.bak-$(date +%Y%m%d-%H%M%S)" || true
/usr/libexec/PlistBuddy -c {shlex.quote("Set :PlatformInfo:Generic:SystemProductName " + sm["SystemProductName"])} "$CFG"
/usr/libexec/PlistBuddy -c {shlex.quote("Set :PlatformInfo:Generic:SystemSerialNumber " + sm["SystemSerialNumber"])} "$CFG"
/usr/libexec/PlistBuddy -c {shlex.quote("Set :PlatformInfo:Generic:MLB " + sm["MLB"])} "$CFG"
/usr/libexec/PlistBuddy -c {shlex.quote("Set :PlatformInfo:Generic:SystemUUID " + sm["SystemUUID"])} "$CFG"
printf '%s' {shlex.quote(rom)} | xxd -r -p > /tmp/rom.bin
/usr/libexec/PlistBuddy -c 'Delete :PlatformInfo:Generic:ROM' "$CFG" || true
/usr/libexec/PlistBuddy -c 'Import :PlatformInfo:Generic:ROM /tmp/rom.bin' "$CFG"
rm -f /tmp/rom.bin
sync
echo PATCHED_OK
"""
    result = await plugin.guest_exec(["/bin/sh", "-c", script], timeout=120.0)
    if result.get("exitcode") not in (0, None) or "PATCHED_OK" not in (result.get("out-data") or ""):
        raise DeploymentError(
            f"OpenCore SMBIOS patch failed: exit={result.get('exitcode')} "
            f"err={result.get('err-data')!r} out={result.get('out-data')!r}"
        )
    try:
        await plugin.guest_exec(["/usr/sbin/diskutil", "unmount", disk_id])
    except Exception as exc:
        logger.warning("OpenCore unmount failed: %s", exc)
    await plugin.update_smbios1(smbios1_config_value(sm, sku=preserved_sku))
    return sm


async def _macos_root_disk_refs(plugin) -> Dict[str, str]:
    """
    Resolve diskutil identifiers for the root volume.

    Returns keys: ``container`` (e.g. disk2), ``physical_store`` (e.g. disk0s2),
    ``whole_disk`` (e.g. disk0).
    """
    import re

    info = await plugin.guest_exec(["/usr/sbin/diskutil", "info", "/"], timeout=60.0)
    out = info.get("out-data") or ""
    container = None
    physical = None
    m = re.search(r"^\s*APFS Container:\s*(disk\d+)\s*$", out, re.MULTILINE)
    if m:
        container = m.group(1)
    m = re.search(r"^\s*APFS Physical Store:\s*(disk\d+s\d+)\s*$", out, re.MULTILINE)
    if m:
        physical = m.group(1)
    if not container:
        m = re.search(r"^\s*Part of Whole:\s*(disk\d+)\s*$", out, re.MULTILINE)
        if m:
            container = m.group(1)
    if not container:
        raise DeploymentError("Could not resolve APFS container for / from diskutil info")
    whole = None
    if physical:
        whole = re.sub(r"s\d+$", "", physical)
    return {
        "container": container,
        "physical_store": physical or "",
        "whole_disk": whole or "",
    }


def _macos_grow_already_done(err: str) -> bool:
    lowered = (err or "").lower()
    return any(
        phrase in lowered
        for phrase in (
            "is already",
            "same size",
            "must be different",
            "no free space",
            "nothing to do",
            "could not find any free space",
            "-69743",
        )
    )


async def grow_macos_root_apfs(plugin) -> None:
    """
    Expand the APFS container for ``/`` to fill newly available disk space.

    Call after the hypervisor disk has been grown (``ConfigureSizingStep``).
    After a VirtIO/QEMU capacity change, macOS often needs
    ``diskutil repairDisk <whole>`` (confirm yes) so the GPT usable range
    matches the new device size; then
    ``diskutil apfs resizeContainer <container> 0``.
    """
    refs = await _macos_root_disk_refs(plugin)
    container = refs["container"]
    whole = refs["whole_disk"]

    if whole:
        repair = await plugin.guest_exec(
            ["/usr/sbin/diskutil", "repairDisk", whole],
            input_data="y\n",
            timeout=180.0,
        )
        logger.info(
            "macOS repairDisk %s exit=%s: %s",
            whole,
            repair.get("exitcode"),
            ((repair.get("out-data") or "") + (repair.get("err-data") or ""))[:240],
        )

    result = await plugin.guest_exec(
        ["/usr/sbin/diskutil", "apfs", "resizeContainer", container, "0"],
        timeout=600.0,
    )
    exitcode = result.get("exitcode")
    err = (result.get("err-data") or "") + " " + (result.get("out-data") or "")
    if exitcode in (0, None) or _macos_grow_already_done(err):
        logger.info(
            "macOS APFS resizeContainer %s 0 done exit=%s: %s",
            container,
            exitcode,
            err[:240],
        )
        return
    raise DeploymentError(
        f"macOS APFS resizeContainer failed: exit={exitcode} err={result.get('err-data')!r} "
        f"out={result.get('out-data')!r}"
    )


async def reboot_guest_and_wait_agent(plugin, timeout: float = 600.0) -> None:
    """Reboot via guest agent shutdown + host start, then wait for agent."""
    from app.plugins.base import PowerState

    try:
        await plugin.guest_shutdown()
    except Exception:
        await plugin.power_off(force=True)
    for _ in range(60):
        state = await plugin.get_power_state()
        if state == PowerState.OFF:
            break
        await asyncio.sleep(2)
    await plugin.power_on()
    await wait_for_agent(plugin, timeout=timeout)
