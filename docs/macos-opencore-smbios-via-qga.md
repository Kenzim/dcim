# Randomize OpenCore SMBIOS via QEMU Guest Agent

Proven on Proxmox host `192.168.11.60`, VM **600** (`macos-tahoe`), 2026-07-24.

**In-app path:** Rackflow’s `macos_guest_agent` deployment strategy and the
`randomize_smbios` strategy action call the same guest-agent flow over the
Proxmox API (`ProxmoxPlugin.guest_exec` / `update_smbios1`). Prefer that for
provisioning; this document remains the ops/reference procedure using `qm`.

## Why this path

Proxmox `qm clone` already rotates hypervisor `smbios1` UUID and NIC MAC. That is **not** enough for macOS: Apple identity comes from OpenCore `config.plist` → `PlatformInfo` → `Generic` (`SystemSerialNumber`, `MLB`, `SystemUUID`, `ROM`, `SystemProductName`).

On VM 600, OpenCore lives on a dedicated EFI disk (`ide0`, guest `disk1s1`, volume name `OPENCORE`), separate from the macOS APFS disk. The guest runs **AppleQEMUGuestAgent** (`/usr/libexec/AppleQEMUGuestAgent`) with:

- `guest-exec` / `guest-exec-status`
- `guest-file-*`
- `guest-shutdown`
- `guest-network-get-interfaces`

So the host can mount the OpenCore volume, rewrite PlatformInfo with `PlistBuddy`, and reboot — no VNC, no mounting the OpenCore disk on the host.

## Prerequisites

1. VM running, agent reachable: `qm agent <vmid> ping`
2. OpenCore on its own disk/partition (not only a shared ISO used by multiple VMs)
3. Host tooling to generate serials (already on epyc): `/root/OSX-PROXMOX/tools/GenSMBIOS/GenSMBIOS.py`
4. Guest has `/usr/libexec/PlistBuddy`, `diskutil`, `xxd`

## Working flow (verified)

### 0. Confirm agent and current identity

```bash
qm agent 600 ping
qm guest exec 600 -- /usr/sbin/system_profiler SPHardwareDataType
```

Before patch, VM 600 reported serial `C02YG0KQHX87`.

### 1. Generate a fresh SMBIOS set on the Proxmox host

```bash
cd /root/OSX-PROXMOX
python3 tools/GenSMBIOS/GenSMBIOS.py --install
python3 tools/GenSMBIOS/GenSMBIOS.py --generate iMacPro1,1 -j /tmp/smbios-600.json
cat /tmp/smbios-600.json
```

Example output used in the successful run:

| Field | Value |
|---|---|
| Type | `iMacPro1,1` |
| Serial | `C02X70YZHX87` |
| Board Serial (MLB) | `C028339024NJG361M` |
| SmUUID | `802168FF-E3DE-489E-A1C1-79BB03D6EB57` |
| Apple ROM | `E48B7F4E9830` |

Optional: check the serial at https://checkcoverage.apple.com/ — prefer “unable to check coverage”.

### 2. Mount OpenCore inside the guest

```bash
qm guest exec 600 -- /usr/sbin/diskutil list
# Look for: EFI OPENCORE  →  disk1s1 (path may differ per VM)

qm guest exec 600 -- /usr/sbin/diskutil mount disk1s1
# → Volume OPENCORE on disk1s1 mounted
```

Config path: `/Volumes/OPENCORE/EFI/OC/config.plist`

### 3. Backup, then patch PlatformInfo

```bash
qm guest exec 600 -- /bin/sh -c '
set -e
CFG=/Volumes/OPENCORE/EFI/OC/config.plist
cp "$CFG" "$CFG.bak-$(date +%Y%m%d-%H%M%S)"

/usr/libexec/PlistBuddy -c "Set :PlatformInfo:Generic:SystemProductName iMacPro1,1" "$CFG"
/usr/libexec/PlistBuddy -c "Set :PlatformInfo:Generic:SystemSerialNumber C02X70YZHX87" "$CFG"
/usr/libexec/PlistBuddy -c "Set :PlatformInfo:Generic:MLB C028339024NJG361M" "$CFG"
/usr/libexec/PlistBuddy -c "Set :PlatformInfo:Generic:SystemUUID 802168FF-E3DE-489E-A1C1-79BB03D6EB57" "$CFG"

# ROM is binary Data — import hex bytes
printf "%s" "E48B7F4E9830" | xxd -r -p > /tmp/rom.bin
/usr/libexec/PlistBuddy -c "Delete :PlatformInfo:Generic:ROM" "$CFG" || true
/usr/libexec/PlistBuddy -c "Import :PlatformInfo:Generic:ROM /tmp/rom.bin" "$CFG"
rm -f /tmp/rom.bin

/usr/libexec/PlistBuddy -c "Print :PlatformInfo:Generic" "$CFG"
sync
'
```

### 4. Align Proxmox `smbios1` (optional but recommended)

Keeps hypervisor SMBIOS roughly consistent with OpenCore:

```bash
qm set 600 --smbios1 "uuid=802168FF-E3DE-489E-A1C1-79BB03D6EB57,base64=1,serial=$(printf %s C02X70YZHX87 | base64 -w0),manufacturer=$(printf %s 'Apple Inc.' | base64 -w0),product=$(printf %s iMacPro1,1 | base64 -w0),family=$(printf %s Mac | base64 -w0)"
```

macOS still trusts OpenCore PlatformInfo for the serial shown in About This Mac; this step is for consistency / tooling.

### 5. Unmount, clean reboot, verify

```bash
qm guest exec 600 -- /usr/sbin/diskutil unmount disk1s1
qm guest cmd 600 shutdown
# wait until stopped
qm start 600
# wait until: qm agent 600 ping

qm guest exec 600 -- /usr/sbin/system_profiler SPHardwareDataType
```

**Verified result on VM 600:** serial changed `C02YG0KQHX87` → `C02X70YZHX87` after reboot. Hardware UUID also changed.

## Automation helper

Repo script: [`scripts/macos_opencore_smbios_qga.py`](../scripts/macos_opencore_smbios_qga.py)

Run from the Proxmox host (or via SSH):

```bash
python3 macos_opencore_smbios_qga.py --vmid 600 --reboot
```

## Layout notes (VM 600)

| Proxmox disk | Guest device | Role |
|---|---|---|
| `ide0` (`vm-600-disk-3`, 1G) | `disk1` / `disk1s1` (`OPENCORE`) | OpenCore EFI + `config.plist` |
| `virtio0` (`vm-600-disk-2`, 64G) | `disk0` APFS | macOS system |
| `efidisk0` | OVMF vars | UEFI vars (not OpenCore PlatformInfo) |

Boot order: `ide0;virtio0` (OpenCore first).

## Caveats

- **Shared OpenCore ISO:** if multiple VMs boot the same `opencore-*.iso`, patching that image changes every VM. Prefer a per-VM OpenCore disk (as on 600 / cloned 501).
- **Reboot required:** OpenCore reads PlatformInfo at boot; live `system_profiler` will not change until after restart.
- **Agent type:** VM 600 uses Apple’s built-in agent (works with default `agent: enabled=1`). Older OpenCore/QEMU guests may need `mac-guest-agent` + `agent: enabled=1,type=isa`.
- **Privileges:** AppleQEMUGuestAgent runs as root and can mount/write the FAT OpenCore volume — confirmed with a write probe and successful plist update.
- **Apple services:** regenerating SMBIOS on a VM already signed into iCloud/iMessage can break those services until reconfigured; do this on golden images / fresh clones before Apple ID login when possible.

## Quick discovery cheat-sheet

```bash
# Is agent alive?
qm agent <vmid> ping

# Which commands exist?
qm agent <vmid> info

# Find OpenCore partition
qm guest exec <vmid> -- /usr/sbin/diskutil list

# Read current OpenCore PlatformInfo
qm guest exec <vmid> -- /bin/sh -c '
diskutil mount disk1s1 >/dev/null
/usr/libexec/PlistBuddy -c "Print :PlatformInfo:Generic" /Volumes/OPENCORE/EFI/OC/config.plist
diskutil unmount disk1s1 >/dev/null
'
```
