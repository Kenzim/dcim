"""
DHCP Configuration Generator

Generates dhcpd.conf file from database configuration and server network port settings.
"""
import logging
import ipaddress
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from sqlalchemy.orm import Session
from app.services.dhcp_config_service import DHCPConfig
from app.dao import NetworkPortDAO, ServerDAO

logger = logging.getLogger(__name__)


def parse_cidr_or_netmask(ip: str, cidr: Optional[int] = None, netmask: Optional[str] = None) -> Tuple[str, str, int]:
    """
    Parse IP with CIDR or netmask and return subnet, netmask, and prefix length.
    
    Args:
        ip: IP address (e.g., "192.168.1.100")
        cidr: CIDR prefix length (e.g., 24 for /24)
        netmask: Netmask (e.g., "255.255.255.0")
    
    Returns:
        Tuple of (subnet, netmask, prefix_length)
    """
    try:
        if cidr is not None:
            # Use CIDR notation
            network = ipaddress.IPv4Network(f"{ip}/{cidr}", strict=False)
            return str(network.network_address), str(network.netmask), cidr
        elif netmask is not None:
            # Use netmask
            network = ipaddress.IPv4Network(f"{ip}/{netmask}", strict=False)
            return str(network.network_address), netmask, network.prefixlen
        else:
            # Default to /24
            network = ipaddress.IPv4Network(f"{ip}/24", strict=False)
            return str(network.network_address), "255.255.255.0", 24
    except Exception as e:
        logger.warning(f"Failed to parse IP {ip} with cidr={cidr}, netmask={netmask}, defaulting to /24: {e}")
        network = ipaddress.IPv4Network(f"{ip}/24", strict=False)
        return str(network.network_address), "255.255.255.0", 24


def ip_in_subnet(ip: str, subnet: str, netmask: str) -> bool:
    """
    Check if an IP address belongs to a subnet.
    
    Args:
        ip: IP address to check
        subnet: Subnet network address
        netmask: Subnet netmask
    
    Returns:
        True if IP is in subnet, False otherwise
    """
    try:
        ip_obj = ipaddress.IPv4Address(ip)
        network = ipaddress.IPv4Network(f"{subnet}/{netmask}", strict=False)
        return ip_obj in network
    except Exception:
        return False


def get_subnet_info_for_client(config: DHCPConfig, client_ip: Optional[str] = None) -> Optional[Dict]:
    """
    Return gateway and netmask for the subnet that contains client_ip.
    Used for static IP kernel param: ip=<client_ip>::<gateway>:<netmask>:<hostname>:<mac>:none

    Args:
        config: DHCP configuration (with interfaces list).
        client_ip: Client/server PXE IP.

    Returns:
        Dict with "gateway" and "netmask", or None if client_ip not in any configured subnet.
    """
    if not client_ip:
        return None
    for iface_config in config.interfaces:
        if isinstance(iface_config, dict):
            ip = iface_config["ip"]
            cidr = iface_config.get("cidr")
            netmask = iface_config.get("netmask")
            gateway = iface_config.get("gateway")
        else:
            ip = iface_config.ip
            cidr = getattr(iface_config, "cidr", None)
            netmask = getattr(iface_config, "netmask", None)
            gateway = getattr(iface_config, "gateway", None)
        subnet, subnet_netmask, _ = parse_cidr_or_netmask(ip, cidr, netmask)
        if ip_in_subnet(client_ip, subnet, subnet_netmask):
            return {"gateway": gateway, "netmask": subnet_netmask}
    return None


def get_next_server_ip_for_client(config: DHCPConfig, client_ip: Optional[str] = None) -> str:
    """
    Return the interface IP (next-server) for the subnet that contains client_ip.
    Used so boot URLs (ISO, kernel, initrd, etc.) use the same host the client
    reaches via DHCP next-server, i.e. the IP from the interfaces JSON for that block.

    Args:
        config: DHCP configuration (with interfaces list).
        client_ip: Client/server PXE IP. If None, returns first interface IP or default.

    Returns:
        IP address string (e.g. "154.13.169.145") to use as base for boot URLs.
    """
    interface_subnets: List[Dict] = []
    for iface_config in config.interfaces:
        if isinstance(iface_config, dict):
            ip = iface_config["ip"]
            cidr = iface_config.get("cidr")
            netmask = iface_config.get("netmask")
        else:
            ip = iface_config.ip
            cidr = getattr(iface_config, "cidr", None)
            netmask = getattr(iface_config, "netmask", None)
        subnet, subnet_netmask, _ = parse_cidr_or_netmask(ip, cidr, netmask)
        interface_subnets.append({"ip": ip, "subnet": subnet, "netmask": subnet_netmask})

    if client_ip and interface_subnets:
        for iface_info in interface_subnets:
            if ip_in_subnet(client_ip, iface_info["subnet"], iface_info["netmask"]):
                return iface_info["ip"]
    if config.interfaces:
        first = config.interfaces[0]
        return first.ip if hasattr(first, "ip") else first["ip"]
    return "192.168.12.74"


def generate_dhcpd_conf(
    config: DHCPConfig, db: Session, location_id: Optional[int] = None, return_content: bool = False
) -> Optional[tuple[str, list[str]]]:
    """
    Generate dhcpd.conf file from configuration and server network ports.

    Args:
        config: DHCP configuration from database
        db: Database session
        location_id: If set, only include servers in this location (for per-location DHCP)
    """
    config_path = Path(config.config_file_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)

    if location_id is not None:
        servers = ServerDAO.get_by_location(db, location_id)
    else:
        servers = ServerDAO.get_all(db, limit=1000, enabled_only=False)
    
    # Build configuration content
    lines = [
        "# DHCP Server Configuration File",
        "# Generated automatically by Rackflow system",
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
    interface_subnets = []  # Store interface info with subnet for next-server lookup
    
    for iface_config in config.interfaces:
        # Access Pydantic model attributes directly
        if isinstance(iface_config, dict):
            interface = iface_config["interface"]
            ip = iface_config["ip"]
            cidr = iface_config.get("cidr")
            netmask = iface_config.get("netmask")
            gateway = iface_config.get("gateway")
        else:
            # Pydantic model
            interface = iface_config.interface
            ip = iface_config.ip
            cidr = iface_config.cidr
            netmask = iface_config.netmask
            gateway = getattr(iface_config, "gateway", None)
        
        # Parse subnet from IP with CIDR or netmask
        subnet, subnet_netmask, prefix_len = parse_cidr_or_netmask(ip, cidr, netmask)
        
        # Store interface info for next-server lookup
        interface_subnets.append({
            "interface": interface,
            "ip": ip,
            "subnet": subnet,
            "netmask": subnet_netmask,
            "prefix_len": prefix_len,
            "gateway": gateway,
        })
        
        # Group by subnet (subnet key includes netmask to handle overlapping subnets with different masks)
        subnet_key = f"{subnet}/{subnet_netmask}"
        if subnet_key not in subnets:
            subnets[subnet_key] = {
                "subnet": subnet,
                "netmask": subnet_netmask,
                "prefix_len": prefix_len,
                "interfaces": []
            }
        
        subnets[subnet_key]["interfaces"].append({
            "interface": interface,
            "ip": ip,
            "gateway": gateway,
        })
    
    # Generate subnet configurations
    for subnet_key, subnet_info in subnets.items():
        subnet = subnet_info['subnet']
        netmask = subnet_info['netmask']
        prefix_len = subnet_info['prefix_len']
        
        lines.append(f"# Subnet configuration for {subnet}/{prefix_len}")
        lines.append(f"subnet {subnet} netmask {netmask} {{")
        
        # Calculate broadcast address
        try:
            network = ipaddress.IPv4Network(f"{subnet}/{netmask}", strict=False)
            broadcast = str(network.broadcast_address)
        except Exception:
            # Fallback calculation for /24
            ip_parts = subnet.split(".")
            broadcast = f"{ip_parts[0]}.{ip_parts[1]}.{ip_parts[2]}.255"
        
        if config.hand_out_leases:
            # Calculate range (exclude reserved IPs)
            # For now, use a simple range - could be improved to exclude all reserved IPs
            try:
                network = ipaddress.IPv4Network(f"{subnet}/{netmask}", strict=False)
                # Use .100 to .200 range, but adjust if subnet is too small
                hosts = list(network.hosts())
                if len(hosts) > 100:
                    range_start = hosts[100] if len(hosts) > 100 else hosts[0]
                    range_end = hosts[200] if len(hosts) > 200 else hosts[-1]
                    lines.append(f"    # Range of IP addresses to lease")
                    lines.append(f"    range {range_start} {range_end};")
                else:
                    # Small subnet, use all available hosts
                    if len(hosts) > 0:
                        lines.append(f"    # Range of IP addresses to lease")
                        lines.append(f"    range {hosts[0]} {hosts[-1]};")
            except Exception:
                # Fallback for /24
                ip_parts = subnet.split(".")
                lines.append(f"    # Range of IP addresses to lease")
                lines.append(f"    range {ip_parts[0]}.{ip_parts[1]}.{ip_parts[2]}.100 {ip_parts[0]}.{ip_parts[1]}.{ip_parts[2]}.200;")
        else:
            lines.append("    # DHCP leases disabled - only serving PXE boot")
        
        lines.append("    ")
        lines.append("    # Subnet mask")
        lines.append(f"    option subnet-mask {netmask};")
        lines.append("    ")
        lines.append("    # Broadcast address")
        lines.append(f"    option broadcast-address {broadcast};")
        # Gateway (routers) for this subnet - servers get this via option routers
        gateway = None
        for iface in subnet_info.get("interfaces", []):
            gw = iface.get("gateway")
            if gw:
                gateway = gw
                break
        if gateway:
            lines.append("    ")
            lines.append("    # Default gateway")
            lines.append(f"    option routers {gateway};")
        dns_servers = getattr(config, "dns_servers", None)
        if dns_servers and len(dns_servers) > 0:
            dns_list = ", ".join(dns_servers)
            lines.append("    ")
            lines.append("    # DNS servers")
            lines.append(f"    option domain-name-servers {dns_list};")
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
        
        # Automatically determine next-server IP based on which subnet the PXE IP belongs to
        api_ip = None
        for iface_info in interface_subnets:
            if ip_in_subnet(pxe_ip, iface_info["subnet"], iface_info["netmask"]):
                api_ip = iface_info["ip"]
                logger.debug(f"Server {server.name} PXE IP {pxe_ip} is in subnet {iface_info['subnet']}/{iface_info['netmask']}, using next-server {api_ip}")
                break
        
        # Fallback to first interface IP if no match found
        if api_ip is None:
            if config.interfaces and len(config.interfaces) > 0:
                first_iface = config.interfaces[0]
                api_ip = first_iface.ip if hasattr(first_iface, 'ip') else first_iface["ip"]
                logger.warning(f"Server {server.name} PXE IP {pxe_ip} not in any configured subnet, using first interface IP {api_ip} as next-server")
            else:
                api_ip = "192.168.12.74"
                logger.warning(f"No interfaces configured, using default next-server IP {api_ip}")
        
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
    
    content = "\n".join(lines)
    interface_names = [
        i.interface if hasattr(i, "interface") else i.get("interface", "eth0") for i in config.interfaces
    ]

    if return_content:
        return content, interface_names

    # Write configuration file
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w") as f:
            f.write(content)
        logger.info(f"Generated dhcpd.conf at {config_path}")
    except Exception as e:
        logger.error(f"Failed to write dhcpd.conf: {e}", exc_info=True)
        raise

    # Write runner_interfaces so the DHCP runner uses the same interfaces as the config
    interfaces_file = config_path.parent / "runner_interfaces"
    try:
        with open(interfaces_file, "w") as f:
            f.write("\n".join(interface_names) + "\n")
    except Exception as e:
        logger.warning("Failed to write runner_interfaces: %s", e)
