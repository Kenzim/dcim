#!/usr/bin/env python3
"""Randomize OpenCore PlatformInfo SMBIOS via QEMU guest agent.

Proven on Proxmox VM 600 (macos-tahoe) using AppleQEMUGuestAgent:
  mount OPENCORE EFI → PlistBuddy patch → optional qm smbios1 sync → reboot.

Run on the Proxmox host (needs qm + GenSMBIOS):

  python3 macos_opencore_smbios_qga.py --vmid 600 --reboot

See docs/macos-opencore-smbios-via-qga.md
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shlex
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_GENSMBIOS = "/root/OSX-PROXMOX/tools/GenSMBIOS/GenSMBIOS.py"
DEFAULT_MODEL = "iMacPro1,1"
DEFAULT_OC_DISK = "disk1s1"
DEFAULT_CFG = "/Volumes/OPENCORE/EFI/OC/config.plist"


def run(cmd: List[str], timeout: Optional[int] = None) -> str:
    return subprocess.check_output(cmd, text=True, timeout=timeout)


def guest_exec(vmid: str, argv: List[str], timeout: int = 180) -> Dict[str, Any]:
    out = run(["qm", "guest", "exec", vmid, "--", *argv], timeout=timeout)
    return json.loads(out)


def must_guest(vmid: str, argv: List[str], timeout: int = 180) -> str:
    data = guest_exec(vmid, argv, timeout=timeout)
    stdout = data.get("out-data") or ""
    stderr = data.get("err-data") or ""
    print("CMD:", " ".join(shlex.quote(a) for a in argv)[:220])
    print("exit:", data.get("exitcode"))
    if stdout:
        print(stdout.rstrip())
    if stderr.strip():
        print("STDERR:", stderr[:2000], file=sys.stderr)
    print("---")
    if data.get("exitcode") not in (0, None):
        raise SystemExit(f"guest-exec failed ({data.get('exitcode')}): {argv}")
    return stdout


def generate_smbios(gensmbios: str, model: str, json_path: str) -> Dict[str, str]:
    subprocess.check_call(["python3", gensmbios, "--install"])
    subprocess.check_call(["python3", gensmbios, "--generate", model, "-j", json_path])
    with open(json_path, encoding="utf-8") as fh:
        data = json.load(fh)
    serial = data.get("Serial") or data.get("SystemSerialNumber")
    mlb = data.get("Board Serial") or data.get("MLB")
    smuuid = data.get("SmUUID") or data.get("SystemUUID")
    rom = data.get("Apple ROM") or data.get("ROM")
    product = data.get("Type") or data.get("SystemProductName") or model
    missing = [k for k, v in {
        "Serial": serial, "MLB": mlb, "SmUUID": smuuid, "ROM": rom,
    }.items() if not v]
    if missing:
        raise SystemExit(f"GenSMBIOS JSON missing fields {missing}: {data}")
    return {
        "SystemProductName": product,
        "SystemSerialNumber": serial,
        "MLB": mlb,
        "SystemUUID": smuuid,
        "ROM_HEX": rom.replace(":", "").lower() if ":" in rom else rom,
    }


def align_proxmox_smbios(vmid: str, sm: Dict[str, str]) -> None:
    smbios = ",".join([
        f"uuid={sm['SystemUUID']}",
        "base64=1",
        f"serial={base64.b64encode(sm['SystemSerialNumber'].encode()).decode()}",
        f"manufacturer={base64.b64encode(b'Apple Inc.').decode()}",
        f"product={base64.b64encode(sm['SystemProductName'].encode()).decode()}",
        f"family={base64.b64encode(b'Mac').decode()}",
    ])
    print("qm set --smbios1", smbios)
    subprocess.check_call(["qm", "set", vmid, "--smbios1", smbios])


def wait_agent(vmid: str, timeout_s: int = 300) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            subprocess.check_call(
                ["qm", "agent", vmid, "ping"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        except subprocess.CalledProcessError:
            time.sleep(5)
    raise SystemExit(f"guest agent on VM {vmid} did not come up within {timeout_s}s")


def wait_stopped(vmid: str, timeout_s: int = 180) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        st = run(["qm", "status", vmid]).strip().split()[-1]
        if st == "stopped":
            return
        time.sleep(3)
    raise SystemExit(f"VM {vmid} did not stop within {timeout_s}s")


def patch_opencore(vmid: str, oc_disk: str, cfg: str, sm: Dict[str, str]) -> None:
    must_guest(vmid, ["/usr/sbin/diskutil", "mount", oc_disk])
    ts = time.strftime("%Y%m%d-%H%M%S")
    must_guest(vmid, ["/bin/sh", "-c", f"cp {shlex.quote(cfg)} {shlex.quote(cfg + '.bak-' + ts)}"])

    lines = [
        "set -e",
        f"CFG={shlex.quote(cfg)}",
        "/usr/libexec/PlistBuddy -c %s \"$CFG\"" % shlex.quote(
            "Set :PlatformInfo:Generic:SystemProductName " + sm["SystemProductName"]
        ),
        "/usr/libexec/PlistBuddy -c %s \"$CFG\"" % shlex.quote(
            "Set :PlatformInfo:Generic:SystemSerialNumber " + sm["SystemSerialNumber"]
        ),
        "/usr/libexec/PlistBuddy -c %s \"$CFG\"" % shlex.quote(
            "Set :PlatformInfo:Generic:MLB " + sm["MLB"]
        ),
        "/usr/libexec/PlistBuddy -c %s \"$CFG\"" % shlex.quote(
            "Set :PlatformInfo:Generic:SystemUUID " + sm["SystemUUID"]
        ),
        f"ROMHEX={shlex.quote(sm['ROM_HEX'])}",
        "printf '%s' \"$ROMHEX\" | xxd -r -p > /tmp/rom.bin",
        "/usr/libexec/PlistBuddy -c 'Delete :PlatformInfo:Generic:ROM' \"$CFG\" || true",
        "/usr/libexec/PlistBuddy -c 'Import :PlatformInfo:Generic:ROM /tmp/rom.bin' \"$CFG\"",
        "rm -f /tmp/rom.bin",
        "echo === PlatformInfo after patch ===",
        "/usr/libexec/PlistBuddy -c 'Print :PlatformInfo:Generic' \"$CFG\"",
        "sync",
    ]
    must_guest(vmid, ["/bin/sh", "-c", "\n".join(lines)])
    must_guest(vmid, ["/usr/sbin/diskutil", "unmount", oc_disk])


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--vmid", required=True, help="Proxmox VMID")
    p.add_argument("--model", default=DEFAULT_MODEL, help="SMBIOS model (default iMacPro1,1)")
    p.add_argument("--oc-disk", default=DEFAULT_OC_DISK, help="Guest OpenCore EFI partition id")
    p.add_argument("--cfg", default=DEFAULT_CFG, help="Guest path to OpenCore config.plist")
    p.add_argument("--gensmbios", default=DEFAULT_GENSMBIOS, help="Host GenSMBIOS.py path")
    p.add_argument("--json-out", default=None, help="Where to write generated SMBIOS JSON")
    p.add_argument("--skip-proxmox-smbios", action="store_true", help="Do not qm set --smbios1")
    p.add_argument("--reboot", action="store_true", help="guest shutdown + qm start + verify")
    p.add_argument("--dry-run-generate", action="store_true", help="Only generate JSON and exit")
    args = p.parse_args()

    if not os.path.exists(args.gensmbios):
        raise SystemExit(f"GenSMBIOS not found: {args.gensmbios}")

    wait_agent(args.vmid, timeout_s=30)
    print("=== Current guest hardware ===")
    must_guest(args.vmid, ["/usr/sbin/system_profiler", "SPHardwareDataType"])

    json_path = args.json_out or f"/tmp/smbios-{args.vmid}.json"
    print("=== Generate SMBIOS ===")
    sm = generate_smbios(args.gensmbios, args.model, json_path)
    print(json.dumps(sm, indent=2))
    if args.dry_run_generate:
        return 0

    print("=== Patch OpenCore via guest agent ===")
    patch_opencore(args.vmid, args.oc_disk, args.cfg, sm)

    if not args.skip_proxmox_smbios:
        print("=== Align Proxmox smbios1 ===")
        align_proxmox_smbios(args.vmid, sm)

    if args.reboot:
        print("=== Reboot ===")
        subprocess.check_call(["qm", "guest", "cmd", args.vmid, "shutdown"])
        wait_stopped(args.vmid)
        subprocess.check_call(["qm", "start", args.vmid])
        wait_agent(args.vmid, timeout_s=300)
        print("=== Post-reboot hardware ===")
        out = must_guest(args.vmid, ["/usr/sbin/system_profiler", "SPHardwareDataType"])
        if sm["SystemSerialNumber"] not in out:
            raise SystemExit(
                f"Serial {sm['SystemSerialNumber']} not visible after reboot — check OpenCore boot disk"
            )
        print("OK: guest reports new serial", sm["SystemSerialNumber"])

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
