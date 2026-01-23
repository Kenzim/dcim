"""
DHCP Configuration Generator

Generates dhcpd.conf file from database configuration and server network port settings.
"""
import logging
from pathlib import Path
from typing import List, Dict
from sqlalchemy.orm import Session
from app.services.dhcp_config_service import DHCPConfig
from app.dao import NetworkPortDAO, ServerDAO

logger = logging.getLogger(__name__)


def generate_dhcpd_conf(config: DHCPConfig, db: Session) -> None:
    """
    Generate dhcpd.conf file from configuration and server network ports.
    
    Args:
        config: DHCP configuration from database
        db: Database session
    """
    config_path = Path(config.config_file_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Get all servers with PXE boot ports
    servers = ServerDAO.get_all(db, limit=1000, enabled_only=False)
    
    # Build configuration content
    lines = [
        "# DHCP Server Configuration File",
        "# Generated automatically by DCIM system",
        f"# DO NOT EDIT MANUALLY - Changes will be overwritten",
        "",
        "# Default lease time (in seconds)",
        f"default-lease-time {config.default_lease_time};",
        "",
        "# Maximum lease time (in seconds)",
        f"max-lease-time {config.max_lease_time};",
        "",
        "# Logging",
        "log-facility local7;",
        "",
        "# Authoritative DHCP server",
        "authoritative;",
        "",
    ]
    
    # Group interfaces by subnet
    subnets = {}
    for iface_config in config.interfaces:
        # Access Pydantic model attributes, not dict keys
        interface = iface_config.interface if hasattr(iface_config, 'interface') else iface_config["interface"]
        ip = iface_config.ip if hasattr(iface_config, 'ip') else iface_config["ip"]
        
        # Extract subnet from IP (assuming /24 for now)
        ip_parts = ip.split(".")
        subnet = f"{ip_parts[0]}.{ip_parts[1]}.{ip_parts[2]}.0"
        
        if subnet not in subnets:
            subnets[subnet] = {
                "netmask": "255.255.255.0",
                "interfaces": []
            }
        
        subnets[subnet]["interfaces"].append({
            "interface": interface,
            "ip": ip
        })
    
    # Generate subnet configurations
    for subnet, subnet_info in subnets.items():
        lines.append(f"# Subnet configuration for {subnet}/24")
        lines.append(f"subnet {subnet} netmask {subnet_info['netmask']} {{")
        
        if config.hand_out_leases:
            # Calculate range (exclude reserved IPs)
            # For now, use a simple range - could be improved to exclude all reserved IPs
            ip_parts = subnet.split(".")
            lines.append(f"    # Range of IP addresses to lease")
            lines.append(f"    range {ip_parts[0]}.{ip_parts[1]}.{ip_parts[2]}.100 {ip_parts[0]}.{ip_parts[1]}.{ip_parts[2]}.200;")
        else:
            lines.append("    # DHCP leases disabled - only serving PXE boot")
        
        lines.append("    ")
        lines.append("    # Subnet mask")
        lines.append(f"    option subnet-mask {subnet_info['netmask']};")
        lines.append("    ")
        lines.append("    # Broadcast address")
        lines.append(f"    option broadcast-address {ip_parts[0]}.{ip_parts[1]}.{ip_parts[2]}.255;")
        lines.append("    ")
        lines.append("    # Default lease time for this subnet")
        lines.append(f"    default-lease-time {config.default_lease_time};")
        lines.append(f"    max-lease-time {config.max_lease_time};")
        lines.append("}")
        lines.append("")
    
    # Generate host reservations for servers with PXE boot ports
    host_num = 1
    for server in servers:
        # Get PXE boot port for this server
        pxe_port = NetworkPortDAO.get_pxe_boot_port(db, server.id)
        
        if not pxe_port or not pxe_port.mac_address or not pxe_port.pxe_ip:
            continue
        
        mac = pxe_port.mac_address.upper().replace("-", ":")
        pxe_ip = pxe_port.pxe_ip
        
        # Determine boot mode and iPXE filename (use pxe_boot_mode for DHCP, fallback to boot_mode for backward compatibility)
        pxe_boot_mode = server.pxe_boot_mode.value if hasattr(server, 'pxe_boot_mode') and server.pxe_boot_mode else (server.boot_mode.value if hasattr(server, 'boot_mode') else "uefi")
        ipxe_filename = "pxe/snponly.efi" if pxe_boot_mode == "uefi" else "pxe/undionly.kpxe"
        
        # Get API server IP (use first interface IP or default)
        if config.interfaces and len(config.interfaces) > 0:
            first_iface = config.interfaces[0]
            api_ip = first_iface.ip if hasattr(first_iface, 'ip') else first_iface["ip"]
        else:
            api_ip = "192.168.12.74"
        
        lines.append(f"# Server: {server.name} (MAC: {mac})")
        lines.append(f"host reserved-host{host_num} {{")
        lines.append(f"    hardware ethernet {mac};")
        lines.append(f"    fixed-address {pxe_ip};")
        lines.append(f"    next-server {api_ip};")
        lines.append(f"    ")
        lines.append(f"    # First boot: Load iPXE loader via TFTP")
        lines.append(f"    filename \"{ipxe_filename}\";")
        lines.append(f"    ")
        lines.append(f"    # When iPXE is already loaded, serve boot script from API")
        lines.append(f"    if exists user-class and option user-class = \"iPXE\" {{")
        lines.append(f"        filename \"http://{api_ip}:8000/api/servers/interaction/pxe?mac=${{mac}}\";")
        lines.append(f"    }}")
        lines.append(f"}}")
        lines.append("")
        
        host_num += 1
    
    # Write configuration file
    try:
        with open(config_path, 'w') as f:
            f.write('\n'.join(lines))
        
        logger.info(f"Generated dhcpd.conf at {config_path}")
    except Exception as e:
        logger.error(f"Failed to write dhcpd.conf: {e}", exc_info=True)
        raise
