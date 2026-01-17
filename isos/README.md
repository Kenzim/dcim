# ISO Images Directory

Place ISO image files in this directory for network booting.

## Usage

ISO files placed here can be booted using boot tasks with `boot_type: "iso"`.

Example:
```json
POST /api/servers/{server_id}/boot-task
{
  "boot_type": "iso",
  "iso_url": "http://192.168.12.74:8000/api/servers/interaction/isos/recovery.iso",
  "description": "Boot recovery ISO"
}
```

## File Naming

- Use descriptive names (e.g., `windows-server-2022.iso`, `ubuntu-22.04-live.iso`, `recovery-tools.iso`)
- Ensure ISO files are accessible via HTTP (the API will serve them)

## Supported ISO Types

- Recovery/Rescue ISOs
- Live CDs/DVDs
- Installation media
- Diagnostic tools
