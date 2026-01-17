# OS Installation Templates

This directory contains OS installation templates. Each template is a self-contained directory with:

- `template.json` - Template configuration (script, disk image, parameters)
- `install.sh` - Installation script that runs to install the OS

## Template Structure

```
template-name/
├── template.json      # Required: Template metadata
├── install.sh        # Required: Installation script
└── [other files]     # Optional: Additional files needed by the template
```

## Template JSON Schema

```json
{
  "id": "unique-template-id",
  "name": "Display Name",
  "description": "Description of what this template does",
  "os_type": "windows|linux|other",
  "script": "install.sh",                    // Script file to run (relative to template directory)
  "disk_image": "disk_images/os-image.iso", // Disk image location (relative to project root)
  "parameters": {
    "param_name": {
      "type": "text|password|select|number|boolean",
      "label": "Display Label",
      "required": true|false,
      "default": "default_value",
      "options": ["option1", "option2"],  // For select type
      "help": "Help text for this parameter"
    }
  },
  "kernel_url": "http://...",  // Optional: For Linux templates
  "initrd_url": "http://..."   // Optional: For Linux templates
}
```

## Key Fields

- **`script`** - The installation script filename (e.g., `install.sh`) that will be executed
- **`disk_image`** - Path to the disk image file (ISO, etc.) relative to project root (e.g., `disk_images/windows-server-2022.iso`)
- **`parameters`** - User-configurable parameters shown in the GUI

## Adding a New Template

1. Create a new directory: `os_templates/my-template-name/`
2. Add `template.json` with metadata
3. Add `install.sh` (or other required files)
4. Restart the backend to scan new templates

## Template Script Variables

Installation scripts can use these variables (replaced in script content):
- `${SERVER_IP}` - Server's IP address
- `${SERVER_MAC}` - Server's MAC address (from PXE boot port)
- `${SERVER_ID}` - Server's database ID
- `${PARAM_*}` - Any template parameters (e.g., `${PARAM_EDITION}`, `${PARAM_ADMIN_PASSWORD}`)

**Note:** For Windows installations, passwords should be written to a file on the NTFS disk during installation. The Windows boot script can then read this file and set the password on first boot. Linux installations can set passwords directly during auto-installation.

## Progress Tracking

Progress is tracked server-side only:
- When a boot task is created, status is `PENDING`
- When the server requests the PXE boot script, status changes to `IN_PROGRESS`
- When the installation completes (or fails), status is updated accordingly

No callbacks are required - the system tracks progress based on server requests.
