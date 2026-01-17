#!/bin/bash
# Install DCIM systemd services
# This script creates systemd service files for DHCP and TFTP servers

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SYSTEMD_DIR="/etc/systemd/system"

echo "Installing DCIM systemd services..."

# Check if running as root
if [ "$EUID" -ne 0 ]; then 
    echo "Please run as root (use sudo)"
    exit 1
fi

# Check if Python is available
if ! command -v python3 &> /dev/null; then
    echo "Error: python3 not found"
    exit 1
fi

# Generate service files using Python
cd "$PROJECT_ROOT"
python3 << 'PYTHON_SCRIPT'
import sys
sys.path.insert(0, '/root/dcim')

from app.services.systemd_service import get_systemd_manager
from app.services.dhcp_config_service import get_dhcp_config_service
from app.services.tftp_config_service import get_tftp_config_service

systemd_manager = get_systemd_manager()

# Get DHCP config
dhcp_config_service = get_dhcp_config_service()
dhcp_config = dhcp_config_service.get_config()

# Generate DHCP service file
dhcp_service_content = systemd_manager.generate_dhcp_service_file(
    dhcp_config.config_file_path,
    dhcp_config.lease_file_path,
    "/root/dcim/dhcpd.pid",
    dhcp_config.interfaces[0].interface if dhcp_config.interfaces else "eth1"
)

# Get TFTP config
tftp_config_service = get_tftp_config_service()
tftp_config = tftp_config_service.get_config()

# Generate TFTP service file
tftp_service_content = systemd_manager.generate_tftp_service_file(
    tftp_config.root_directory,
    tftp_config.bind_address,
    tftp_config.bind_port,
    tftp_config.ipv4_only,
    tftp_config.allow_create,
    tftp_config.verbose
)

# Install services
dhcp_installed = systemd_manager.install_service("dcim-dhcpd.service", dhcp_service_content)
tftp_installed = systemd_manager.install_service("dcim-tftpd.service", tftp_service_content)

if dhcp_installed:
    print("✓ DHCP service installed")
else:
    print("✗ Failed to install DHCP service")
    sys.exit(1)

if tftp_installed:
    print("✓ TFTP service installed")
else:
    print("✗ Failed to install TFTP service")
    sys.exit(1)

print("\nServices installed successfully!")
print("\nTo enable services to start on boot:")
print("  sudo systemctl enable dcim-dhcpd.service")
print("  sudo systemctl enable dcim-tftpd.service")
print("\nTo start services now:")
print("  sudo systemctl start dcim-dhcpd.service")
print("  sudo systemctl start dcim-tftpd.service")
PYTHON_SCRIPT

echo ""
echo "Installation complete!"
