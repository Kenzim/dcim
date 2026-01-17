# Installation Configurations

This directory contains installation configuration files that define the **workflow and process** for installing operating systems.

## Purpose

**`installation_configs/`** defines **HOW** the installation process works:
- Installation steps and workflow
- Where configuration files should be written (e.g., `C:/dcim_password.txt`)
- What parameters are required
- The sequence of operations

## Relationship to `os_templates/`

- **`os_templates/`** = The **executable code** (scripts, templates, files) that actually performs the installation
- **`installation_configs/`** = The **workflow definition** that describes the installation process

Think of it like:
- `os_templates/` = The chef and recipe (does the work)
- `installation_configs/` = The cooking instructions (describes the steps)

## Structure

Each installation configuration is a single JSON file named after the OS and version:
- `windows_2022.json` - Windows Server 2022 installation workflow
- `windows_2025.json` - Windows Server 2025 installation workflow
- `ubuntu.json` - Ubuntu installation workflow
- etc.

## Example: Windows Server 2022

The `windows_2022.json` file defines:
- **Steps**: Write template → Mount filesystem → Write password config → Boot
- **Config file locations**: Where to write password (`C:/dcim_password.txt`) and config (`C:/dcim_config.json`)
- **Required parameters**: What user input is needed (admin_password, hostname)

## Current Status

**Note:** This directory is currently **not yet integrated** into the system. The actual installation system currently uses `os_templates/` which contains both the workflow and executable scripts.

This directory is prepared for future use where we might want to:
- Separate workflow definitions from executable code
- Have multiple workflows reference the same template
- Define installation processes more declaratively
