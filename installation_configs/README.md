# Installation Configurations

This directory contains installation configuration files for different operating systems.

## Structure

Each installation configuration should have:
- `config.json` - Configuration metadata and parameters
- Installation scripts and templates as needed

## Windows Installation

The Windows installation configuration (`windows/config.json`) defines:
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
1. Create a new directory under `installation_configs/`
2. Add a `config.json` file with the configuration metadata
3. Add any required installation scripts or templates
