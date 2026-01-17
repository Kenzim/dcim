# Installation Configurations

This directory contains installation configuration files for different operating systems.

## Structure

Each installation configuration is a single JSON file named after the OS and version:
- `windows_2022.json` - Windows Server 2022 installation
- `windows_2025.json` - Windows Server 2025 installation
- `ubuntu.json` - Ubuntu installation
- etc.

## Windows Server 2022 Installation

The Windows Server 2022 installation configuration (`windows_2022.json`) defines:
- Installation steps (write template, mount filesystem, write password config, boot)
- Configuration file locations (C:/dcim_password.txt, C:/dcim_config.json)
- Required parameters (admin_password, hostname)

The installation process will:
1. Write the Windows template to the target disk
2. Mount the newly created Windows filesystem
3. Write the administrator password to `C:/dcim_password.txt`
4. Write additional configuration to `C:/dcim_config.json`
5. Reboot and boot into Windows

## Adding New Configurations

To add a new installation configuration:
1. Create a new JSON file named `{os}_{version}.json` (e.g., `ubuntu_22.04.json`)
2. Follow the structure of existing configuration files
3. Add any required installation scripts or templates to the `os_templates/` directory
