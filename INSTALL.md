# DCIM Installation Guide

## System Requirements

- Linux system with systemd
- Python 3.11+
- MySQL/MariaDB database
- Root or sudo access for service installation

## Installation Steps

### 1. Install Python Dependencies

```bash
cd /root/dcim
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure Database

Edit `app/core/config.py` or set environment variables for database connection.

### 3. Run Database Migrations

```bash
alembic upgrade head
```

### 4. Install Systemd Services

The DHCP and TFTP services need to be installed as systemd services:

```bash
sudo ./scripts/install_services.sh
```

This will:
- Create `/etc/systemd/system/dcim-dhcpd.service`
- Create `/etc/systemd/system/dcim-tftpd.service`
- Reload systemd daemon

### 5. (Optional) Enable Services to Start on Boot

```bash
sudo systemctl enable dcim-dhcpd.service
sudo systemctl enable dcim-tftpd.service
```

### 6. Start the Application

```bash
source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Or use a process manager like systemd, supervisor, or PM2.

## Service Management

### Start/Stop Services via GUI

The web interface provides controls to start, stop, and restart DHCP and TFTP services.

### Start/Stop Services via Command Line

```bash
# DHCP
sudo systemctl start dcim-dhcpd.service
sudo systemctl stop dcim-dhcpd.service
sudo systemctl restart dcim-dhcpd.service
sudo systemctl status dcim-dhcpd.service

# TFTP
sudo systemctl start dcim-tftpd.service
sudo systemctl stop dcim-tftpd.service
sudo systemctl restart dcim-tftpd.service
sudo systemctl status dcim-tftpd.service
```

## Service Configuration

Service configurations are stored in:
- `/root/dcim/dhcp_config.json` - DHCP server configuration
- `/root/dcim/tftp_config.json` - TFTP server configuration

These can be edited via the web interface (Services tab) or manually. After changing configuration, the service files will be automatically regenerated when you start/restart the services through the GUI.

## Notes

- Services are created dynamically when started through the GUI if they don't exist
- Service files are automatically updated when configuration changes
- The installation script uses the current configuration files to generate service files
- Services run independently of the main application and persist across app restarts
