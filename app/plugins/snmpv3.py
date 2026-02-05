"""
SNMPv3 read-only plugin for network switch management.

This plugin provides read-only access to switch information and statistics.
It does NOT support any write operations (port control, VLAN management, etc.).

Supports:
- Port listing via IF-MIB
- Port statistics and bandwidth monitoring via IF-MIB
- Switch information (model, serial, firmware, port count)

Read-only operations:
- test_connection: Test SNMPv3 connectivity
- get_switch_info: Get switch model, serial, firmware, port count
- get_all_port_statistics: Get statistics for all ports
- get_port_statistics: Get statistics for a specific port

Note: This plugin only supports MONITORING category (read-only).
For port control or configuration changes, use a plugin that supports PORT_CONTROL.
"""
import logging
import re
import sys
from typing import Dict, Any, List
from pysnmp.hlapi.asyncio import (
    SnmpEngine,
    UsmUserData,
    UdpTransportTarget,
    ContextData,
    get_cmd,
    next_cmd,
    ObjectType,
    ObjectIdentity,
    usmHMACMD5AuthProtocol,
    usmHMACSHAAuthProtocol,
    usmHMAC128SHA224AuthProtocol,
    usmHMAC192SHA256AuthProtocol,
    usmHMAC256SHA384AuthProtocol,
    usmHMAC384SHA512AuthProtocol,
    usmNoPrivProtocol,
    usmDESPrivProtocol,
    usmAesBlumenthalCfb192Protocol,
    usmAesBlumenthalCfb256Protocol,
    usmAesCfb128Protocol,
    usmAesCfb192Protocol,
    usmAesCfb256Protocol,
)
from app.plugins.switch_base import SwitchPlugin, SwitchPluginCategory

logger = logging.getLogger(__name__)


class SNMPv3Plugin(SwitchPlugin):
    """
    SNMPv3 read-only plugin for network switch management.
    
    This plugin provides read-only access to switch information and statistics.
    It does NOT support any write operations (port control, VLAN management, etc.).
    
    Supports:
    - MONITORING: Get port list, bandwidth statistics, and switch info via IF-MIB
    
    Configuration:
    - hostname: Switch IP address or hostname
    - username: SNMPv3 username
    - auth_protocol: Authentication protocol (MD5, SHA, SHA224, SHA256, SHA384, SHA512)
    - auth_password: Authentication password
    - priv_protocol: Privacy protocol (DES, AES128, AES192, AES256, AES128BLMT, AES192BLMT, AES256BLMT)
    - priv_password: Privacy password
    - security_level: Security level (noAuthNoPriv, authNoPriv, authPriv)
    - port: SNMP port (default: 161)
    
    Note: This plugin is read-only. For port control or configuration changes,
    use a plugin that supports PORT_CONTROL category.
    """
    
    PLUGIN_NAME = "snmpv3"
    PLUGIN_VERSION = "1.0.0"
    SUPPORTED_CATEGORIES = [
        SwitchPluginCategory.MONITORING,  # Read-only: port statistics and monitoring only
    ]
    
    CONFIG_TEMPLATE = {
        "type": "object",
        "required": ["hostname", "username", "security_level"],
        "properties": {
            "hostname": {
                "type": "string",
                "title": "Hostname/IP",
                "description": "Switch IP address or hostname",
                "required": True
            },
            "username": {
                "type": "string",
                "title": "Username",
                "description": "SNMPv3 username (pysnmp library requires at least 8 characters)",
                "minLength": 8,
                "required": True
            },
            "auth_protocol": {
                "type": "string",
                "title": "Auth Protocol",
                "description": "Authentication protocol",
                "enum": ["MD5", "SHA", "SHA224", "SHA256", "SHA384", "SHA512"],
                "default": "SHA"
            },
            "auth_password": {
                "type": "string",
                "title": "Auth Password",
                "description": "Authentication password (required for authNoPriv and authPriv)",
                "format": "password"
            },
            "priv_protocol": {
                "type": "string",
                "title": "Privacy Protocol",
                "description": "Privacy protocol (required for authPriv)",
                "enum": ["DES", "AES128", "AES192", "AES256", "AES128BLMT", "AES192BLMT", "AES256BLMT"],
                "default": "AES128"
            },
            "priv_password": {
                "type": "string",
                "title": "Privacy Password",
                "description": "Privacy password (required for authPriv)",
                "format": "password"
            },
            "security_level": {
                "type": "string",
                "title": "Security Level",
                "description": "SNMPv3 security level",
                "enum": ["noAuthNoPriv", "authNoPriv", "authPriv"],
                "default": "authPriv",
                "required": True
            },
            "port": {
                "type": "integer",
                "title": "SNMP Port",
                "description": "SNMP port number",
                "default": 161,
                "minimum": 1,
                "maximum": 65535
            }
        }
    }
    
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.hostname = config.get("hostname")
        self.port = config.get("port", 161)
        self.username = config.get("username")
        self.security_level = config.get("security_level", "authPriv")
        
        # Validate username
        # Note: pysnmp library enforces a minimum of 8 characters for SNMPv3 usernames
        # This is a library constraint, not an SNMPv3 RFC requirement
        if not self.username:
            raise ValueError("SNMPv3 username is required")
        
        if len(self.username) < 8:
            raise ValueError(
                f"SNMPv3 username must be at least 8 characters long (pysnmp library requirement). "
                f"Current username '{self.username}' is only {len(self.username)} characters. "
                f"Please use a longer username (e.g., 'admin123' or 'snmpuser')."
            )
        
        # Build SNMP user data based on security level
        self._build_user_data()
    
    def _build_user_data(self):
        """Build UsmUserData object based on configuration"""
        auth_protocol_map = {
            "MD5": usmHMACMD5AuthProtocol,
            "SHA": usmHMACSHAAuthProtocol,
            "SHA224": usmHMAC128SHA224AuthProtocol,
            "SHA256": usmHMAC192SHA256AuthProtocol,
            "SHA384": usmHMAC256SHA384AuthProtocol,
            "SHA512": usmHMAC384SHA512AuthProtocol,
        }
        
        priv_protocol_map = {
            "DES": usmDESPrivProtocol,
            "AES128": usmAesCfb128Protocol,
            "AES192": usmAesCfb192Protocol,
            "AES256": usmAesCfb256Protocol,
            "AES128BLMT": usmAesBlumenthalCfb192Protocol,  # Note: Blumenthal variants
            "AES192BLMT": usmAesBlumenthalCfb192Protocol,
            "AES256BLMT": usmAesBlumenthalCfb256Protocol,
        }
        
        if self.security_level == "noAuthNoPriv":
            self.user_data = UsmUserData(
                self.username
            )
        elif self.security_level == "authNoPriv":
            auth_protocol = auth_protocol_map.get(
                self.config.get("auth_protocol", "SHA"),
                usmHMACSHAAuthProtocol
            )
            auth_password = self.config.get("auth_password", "")
            self.user_data = UsmUserData(
                self.username,
                authKey=auth_password,
                authProtocol=auth_protocol,
                privKey=None,
                privProtocol=usmNoPrivProtocol
            )
        else:  # authPriv
            auth_protocol = auth_protocol_map.get(
                self.config.get("auth_protocol", "SHA"),
                usmHMACSHAAuthProtocol
            )
            priv_protocol = priv_protocol_map.get(
                self.config.get("priv_protocol", "AES128"),
                usmAesCfb128Protocol
            )
            auth_password = self.config.get("auth_password", "")
            priv_password = self.config.get("priv_password", "")
            self.user_data = UsmUserData(
                self.username,
                authKey=auth_password,
                authProtocol=auth_protocol,
                privKey=priv_password,
                privProtocol=priv_protocol
            )
    
    async def _get_transport(self):
        """Get transport target with timeout so SNMP calls don't hang indefinitely"""
        return await UdpTransportTarget.create(
            (self.hostname, self.port),
            timeout=30.0,
            retries=2
        )
    
    async def test_connection(self) -> Dict[str, Any]:
        """
        Test SNMPv3 connection to the switch.
        
        Tests by querying sysDescr (1.3.6.1.2.1.1.1.0) from SNMPv2-MIB.
        """
        # Validate configuration before attempting connection
        if not self.hostname:
            return {
                "success": False,
                "message": "Hostname is required",
                "details": {"error_type": "configuration"}
            }
        
        if not self.username:
            return {
                "success": False,
                "message": "Username is required",
                "details": {"error_type": "configuration"}
            }
        
        if len(self.username) < 8:
            return {
                "success": False,
                "message": f"SNMPv3 username must be at least 8 characters long (pysnmp library requirement). Current username '{self.username}' is only {len(self.username)} characters.",
                "details": {"error_type": "configuration", "username_length": len(self.username), "note": "This is a pysnmp library constraint, not an SNMPv3 RFC requirement"}
            }
        
        try:
            # Query sysDescr to test connection
            snmp_engine = SnmpEngine()
            error_indication, error_status, error_index, var_binds = await get_cmd(
                snmp_engine,
                self.user_data,
                await self._get_transport(),
                ContextData(),
                ObjectType(ObjectIdentity("1.3.6.1.2.1.1.1.0"))  # sysDescr
            )
            
            if error_indication:
                error_str = str(error_indication)
                # Provide more helpful error messages for common SNMPv3 errors
                if "ciphering" in error_str.lower() or "ciphertext" in error_str.lower():
                    message = (
                        "SNMPv3 privacy/encryption error: The privacy protocol or password is incorrect, "
                        "or the device doesn't support the selected privacy protocol.\n\n"
                        f"Current configuration:\n"
                        f"- Security level: {self.security_level}\n"
                        f"- Privacy protocol: {self.config.get('priv_protocol', 'Not set')}\n\n"
                        "Troubleshooting:\n"
                        "1. Verify the privacy password is correct\n"
                        "2. Check if the device supports the selected privacy protocol (try AES128 or DES)\n"
                        "3. If the device doesn't support encryption, try security level 'authNoPriv' instead of 'authPriv'"
                    )
                elif "authentication" in error_str.lower() or "auth" in error_str.lower():
                    message = (
                        "SNMPv3 authentication error: Authentication failed.\n\n"
                        f"Current configuration:\n"
                        f"- Username: {self.username}\n"
                        f"- Security level: {self.security_level}\n"
                        f"- Auth protocol: {self.config.get('auth_protocol', 'Not set')}\n\n"
                        "Troubleshooting:\n"
                        "1. Verify the username and authentication password are correct\n"
                        "2. Check if the device supports the selected authentication protocol (try SHA or MD5)\n"
                        "3. Ensure the security level matches the device configuration"
                    )
                elif "timeout" in error_str.lower() or "timed out" in error_str.lower():
                    message = (
                        f"SNMPv3 connection timeout: Could not reach {self.hostname}:{self.port}\n\n"
                        "Troubleshooting:\n"
                        "1. Verify the hostname/IP address is correct\n"
                        "2. Check network connectivity (ping the device)\n"
                        "3. Ensure SNMP is enabled on the device\n"
                        "4. Verify the SNMP port (default: 161)"
                    )
                elif "no such name" in error_str.lower() or "unknown" in error_str.lower():
                    message = (
                        f"SNMPv3 error: Device not found or SNMP not enabled on {self.hostname}:{self.port}\n\n"
                        "Troubleshooting:\n"
                        "1. Verify the hostname/IP address is correct\n"
                        "2. Ensure SNMPv3 is enabled on the device\n"
                        "3. Check if the device is reachable"
                    )
                else:
                    message = f"SNMP error: {error_indication}"
                
                return {
                    "success": False,
                    "message": message,
                    "details": {
                        "error_type": "error_indication",
                        "raw_error": error_str,
                        "security_level": self.security_level,
                        "priv_protocol": self.config.get("priv_protocol"),
                        "auth_protocol": self.config.get("auth_protocol")
                    }
                }
            
            if error_status:
                return {
                    "success": False,
                    "message": f"SNMP error: {error_status.prettyPrint()} at {error_index and var_binds[int(error_index) - 1][0] or '?'}",
                    "details": {"error_status": error_status.prettyPrint()}
                }
            
            # Extract sysDescr value
            sys_descr = ""
            for oid, val in var_binds:
                sys_descr = str(val)
            
            return {
                "success": True,
                "message": "SNMPv3 connection successful",
                "details": {
                    "sysDescr": sys_descr,
                    "hostname": self.hostname,
                    "port": self.port,
                    "username": self.username,
                    "security_level": self.security_level
                }
            }
        except ValueError as e:
            # Configuration validation errors
            logger.warning(f"SNMPv3 configuration error: {e}")
            return {
                "success": False,
                "message": str(e),
                "details": {"error_type": "configuration", "exception": str(e)}
            }
        except Exception as e:
            logger.exception("SNMPv3 connection test failed")
            error_msg = str(e)
            # Provide more helpful error messages for common issues
            if "ValueConstraintError" in error_msg or "WrongValueError" in error_msg:
                if "username" in error_msg.lower() or len(self.username) < 8:
                    error_msg = f"SNMPv3 username '{self.username}' is invalid. Username must be at least 8 characters long."
                elif "password" in error_msg.lower() or "auth" in error_msg.lower():
                    error_msg = f"SNMPv3 authentication failed. Please check your auth_password and security_level settings."
                else:
                    error_msg = f"SNMPv3 configuration error: {error_msg}"
            
            return {
                "success": False,
                "message": error_msg,
                "details": {"exception": str(e), "error_type": type(e).__name__}
            }
    
    async def get_switch_info(self) -> Dict[str, Any]:
        """
        Get switch information via SNMP.
        
        Queries:
        - sysDescr (1.3.6.1.2.1.1.1.0) - System description (contains model/firmware info)
        - sysObjectID (1.3.6.1.2.1.1.2.0) - System object ID (identifies model)
        - entPhysicalSerialNum (1.3.6.1.2.1.47.1.1.1.1.11.1) - Serial number from Entity MIB
        - Port count from IF-MIB (count physical interfaces)
        """
        try:
            snmp_engine = SnmpEngine()
            info = {
                "model": None,
                "serial_number": None,
                "firmware_version": None,
                "port_count": 0
            }
            
            # Query sysDescr (system description)
            try:
                error_ind, error_stat, error_idx, var_bnds = await get_cmd(
                    snmp_engine,
                    self.user_data,
                    await self._get_transport(),
                    ContextData(),
                    ObjectType(ObjectIdentity("1.3.6.1.2.1.1.1.0"))  # sysDescr
                )
                if not error_ind and not error_stat:
                    for oid, val in var_bnds:
                        sys_descr = str(val)
                        # sysDescr often contains model and firmware info
                        # Try to extract model and firmware from description
                        info["model"] = sys_descr.split(",")[0].strip() if sys_descr else None
                        # Look for version patterns in sysDescr
                        version_match = re.search(r'Version\s+([^\s,]+)', sys_descr, re.IGNORECASE)
                        if version_match:
                            info["firmware_version"] = version_match.group(1)
                        elif not info["firmware_version"]:
                            # Try other patterns
                            version_match = re.search(r'v?(\d+\.\d+[^\s,]*)', sys_descr)
                            if version_match:
                                info["firmware_version"] = version_match.group(1)
            except Exception as e:
                logger.debug(f"Error querying sysDescr: {e}")
            
            # Query sysObjectID to help identify model
            try:
                error_ind, error_stat, error_idx, var_bnds = await get_cmd(
                    snmp_engine,
                    self.user_data,
                    await self._get_transport(),
                    ContextData(),
                    ObjectType(ObjectIdentity("1.3.6.1.2.1.1.2.0"))  # sysObjectID
                )
                if not error_ind and not error_stat:
                    for oid, val in var_bnds:
                        sys_obj_id = str(val)
                        # sysObjectID can help identify the model
                        # Common OIDs:
                        # Cisco: 1.3.6.1.4.1.9.1.*
                        # HP: 1.3.6.1.4.1.11.2.3.7.11.*
                        # etc.
                        logger.debug(f"sysObjectID: {sys_obj_id}")
            except Exception as e:
                logger.debug(f"Error querying sysObjectID: {e}")
            
            # Try to get serial number from Entity MIB
            try:
                # entPhysicalSerialNum.1 (first physical entity)
                error_ind, error_stat, error_idx, var_bnds = await get_cmd(
                    snmp_engine,
                    self.user_data,
                    await self._get_transport(),
                    ContextData(),
                    ObjectType(ObjectIdentity("1.3.6.1.2.1.47.1.1.1.1.11.1"))  # entPhysicalSerialNum.1
                )
                if not error_ind and not error_stat:
                    for oid, val in var_bnds:
                        serial = str(val).strip()
                        if serial and serial != "":
                            info["serial_number"] = serial
            except Exception as e:
                logger.debug(f"Error querying serial number: {e}")
            
            # Get port count by counting physical Ethernet interfaces from IF-MIB
            # Walk ifType OID directly and count only interfaces with ifType 6 (ethernetCsmacd)
            try:
                port_count = 0
                var_binds = [ObjectType(ObjectIdentity("1.3.6.1.2.1.2.2.1.3"))]  # ifType - start walk here
                
                while True:
                    # Limit to reasonable number to avoid infinite loops
                    if port_count >= 1000:
                        break
                    
                    error_indication, error_status, error_index, var_binds = await next_cmd(
                        snmp_engine,
                        self.user_data,
                        await self._get_transport(),
                        ContextData(),
                        *var_binds,
                        lexicographicMode=False
                    )
                    
                    if error_indication or error_status:
                        # End of walk or error - break
                        break
                    
                    # Extract ifType value from this row
                    # var_binds contains tuples of (OID, value) for each OID in the walk
                    for oid, val in var_binds:
                        # Check if this is still within the ifType OID tree (1.3.6.1.2.1.2.2.1.3.*)
                        oid_str = str(oid)
                        if not oid_str.startswith("1.3.6.1.2.1.2.2.1.3"):
                            # We've walked past the ifType OID tree, we're done
                            break
                        
                        try:
                            if_type = int(val) if val is not None else 0
                            # Only count physical Ethernet ports (ifType 6 = ethernetCsmacd)
                            if if_type == 6:
                                port_count += 1
                        except (ValueError, TypeError):
                            # Skip if we can't parse the value
                            continue
                    else:
                        # All OIDs were within ifType tree, continue walking
                        continue
                    
                    # If we broke from the inner loop, we've left the ifType tree
                    break
                
                info["port_count"] = port_count
            except Exception as e:
                logger.debug(f"Error counting ports: {e}")
            
            return info
            
        except Exception as e:
            logger.exception("Failed to get switch info")
            raise Exception(f"Failed to get switch info: {str(e)}")
    
    async def get_all_port_statistics(self) -> Dict[str, Dict[str, Any]]:
        """
        Get statistics for all ports using IF-MIB.
        
        Uses the same walk as get_switch_info (walk ifType) so behavior matches
        the test connection. Collects (if_index, if_type) from the walk, then
        one GET (11 OIDs) per physical Ethernet interface.
        
        Returns a dictionary mapping port identifiers to their statistics.
        """
        try:
            ports = {}
            logger.info(f"SNMP get_all_port_statistics: starting for {self.hostname}:{self.port}")
            sys.stderr.flush()
            snmp_engine = SnmpEngine()
            base_oid = "1.3.6.1.2.1.2.2.1"
            # IF-MIB ifTable: column.index (e.g. .2.<i> = ifDescr, .3.<i> = ifType)
            if_type_oid = "1.3.6.1.2.1.2.2.1.3"
            # IF-MIB ifXTable: ifHighSpeed (Mbps) gives accurate speed for >4 Gbps links
            if_high_speed_oid_prefix = "1.3.6.1.2.1.31.1.1.1.15"
            oid_keys = [
                ("2", "name"), ("3", "ifType"), ("5", "ifSpeed"),
                ("7", "ifAdminStatus"), ("8", "ifOperStatus"),
                ("10", "ifInOctets"), ("16", "ifOutOctets"),
                ("13", "ifInDiscards"), ("14", "ifInErrors"),
                ("19", "ifOutDiscards"), ("20", "ifOutErrors"),
            ]
            
            # Step 1: Walk ifType exactly like get_switch_info (same loop, same transport pattern)
            ethernet_indices = []
            var_binds = [ObjectType(ObjectIdentity(if_type_oid))]
            walk_steps = 0
            logger.info("SNMP get_all_port_statistics: walking ifType (same as get_switch_info)...")
            sys.stderr.flush()
            
            while True:
                if len(ethernet_indices) >= 1000:
                    break
                walk_steps += 1
                if walk_steps == 1:
                    logger.info("SNMP get_all_port_statistics: first next_cmd (walk ifType)...")
                    sys.stderr.flush()
                elif walk_steps % 20 == 0:
                    logger.info(f"SNMP get_all_port_statistics: walk step {walk_steps}, {len(ethernet_indices)} ethernet indices so far")
                    sys.stderr.flush()
                error_indication, error_status, error_index, var_binds = await next_cmd(
                    snmp_engine,
                    self.user_data,
                    await self._get_transport(),
                    ContextData(),
                    *var_binds,
                    lexicographicMode=False
                )
                if error_indication:
                    logger.warning(f"SNMP error indication: {error_indication}")
                    break
                if error_status:
                    logger.warning(f"SNMP error status: {error_status.prettyPrint()}")
                    break
                for oid, val in var_binds:
                    oid_str = str(oid)
                    if not oid_str.startswith(if_type_oid + "."):
                        break
                    try:
                        # OID is 1.3.6.1.2.1.2.2.1.3.<index>; last component is if_index
                        if_index = int(oid_str.split(".")[-1])
                        if_type = int(val) if val is not None else 0
                        if if_type == 6:  # ethernetCsmacd
                            ethernet_indices.append(if_index)
                    except (ValueError, TypeError, IndexError):
                        continue
                else:
                    # Still in ifType tree, continue walk
                    continue
                # Left ifType tree
                break
            
            logger.info(f"SNMP get_all_port_statistics: walk done, {len(ethernet_indices)} physical Ethernet indices")
            sys.stderr.flush()
            
            # Step 2: One GET per interface - same get_cmd pattern as test_connection
            logger.info(f"SNMP get_all_port_statistics: fetching stats for {len(ethernet_indices)} interfaces (one GET per interface)...")
            sys.stderr.flush()
            for idx, if_index in enumerate(ethernet_indices):
                # Core IF-MIB columns from ifTable
                get_oids = [ObjectType(ObjectIdentity(f"{base_oid}.{suf}.{if_index}")) for suf, _ in oid_keys]
                # Plus ifHighSpeed (Mbps) from ifXTable for high-speed links
                get_oids.append(
                    ObjectType(ObjectIdentity(f"{if_high_speed_oid_prefix}.{if_index}"))
                )
                try:
                    error_ind, error_stat, error_idx, var_bnds = await get_cmd(
                        snmp_engine,
                        self.user_data,
                        await self._get_transport(),
                        ContextData(),
                        *get_oids
                    )
                except Exception as e:
                    logger.debug(f"Error get_cmd for ifIndex {if_index}: {e}")
                    continue
                if error_ind or error_stat:
                    continue
                port_data = {
                    "ifIndex": if_index,
                    "name": "",
                    "ifType": 0,
                    "ifSpeed": 0,
                    "ifHighSpeed": 0,
                    "ifAdminStatus": 0,
                    "ifOperStatus": 0,
                    "ifInOctets": 0,
                    "ifOutOctets": 0,
                    "ifInErrors": 0,
                    "ifOutErrors": 0,
                    "ifInDiscards": 0,
                    "ifOutDiscards": 0,
                }
                # Map core columns from ifTable
                for i, (_, key) in enumerate(oid_keys):
                    if i >= len(var_bnds):
                        break
                    _, val = var_bnds[i]
                    if key == "name":
                        port_data[key] = str(val)
                    else:
                        try:
                            port_data[key] = int(val) if val is not None else 0
                        except (ValueError, TypeError):
                            port_data[key] = 0
                # Last varbind is ifHighSpeed (Mbps) from ifXTable; tolerate devices that don't support it
                if len(var_bnds) > len(oid_keys):
                    _, val = var_bnds[len(oid_keys)]
                    try:
                        port_data["ifHighSpeed"] = int(val) if val is not None else 0
                    except (ValueError, TypeError):
                        port_data["ifHighSpeed"] = 0
                port_key = port_data["name"] if port_data["name"] else f"ifIndex-{if_index}"
                ports[port_key] = port_data
                if (idx + 1) % 10 == 0:
                    logger.info(f"SNMP get_all_port_statistics: collected {idx + 1}/{len(ethernet_indices)} interfaces")
                    sys.stderr.flush()
            
            logger.info(f"SNMP get_all_port_statistics: done for {self.hostname}:{self.port} — {len(ports)} interfaces")
            sys.stderr.flush()
            return ports
            
        except Exception as e:
            logger.exception("Failed to get port statistics")
            raise Exception(f"Failed to get port statistics: {str(e)}")
    
    async def get_port_statistics(self, port: str) -> Dict[str, Any]:
        """
        Get statistics for a specific port (read-only).
        
        Args:
            port: Port identifier (port name or ifIndex)
        
        Returns:
            Dict with port statistics
        
        Note: This is a read-only operation. This plugin does not support
        port control or configuration changes.
        """
        all_stats = await self.get_all_port_statistics()
        
        # Try to find by name first, then by ifIndex
        if port in all_stats:
            return all_stats[port]
        
        # Try to find by ifIndex
        for port_name, stats in all_stats.items():
            if str(stats.get("ifIndex")) == port:
                return stats
        
        raise ValueError(f"Port '{port}' not found")
    
    # Explicitly override any write methods to ensure read-only behavior
    async def enable_port(self, port: str) -> Dict[str, Any]:
        """Not supported - this is a read-only plugin"""
        raise NotImplementedError("Port control is not supported by this read-only SNMPv3 plugin")
    
    async def disable_port(self, port: str) -> Dict[str, Any]:
        """Not supported - this is a read-only plugin"""
        raise NotImplementedError("Port control is not supported by this read-only SNMPv3 plugin")
    
    async def get_port_status(self, port: str) -> Dict[str, Any]:
        """
        Get port status (read-only).
        
        This method provides read-only status information.
        For actual port control, use a plugin that supports PORT_CONTROL category.
        """
        # Return statistics which include operational status
        stats = await self.get_port_statistics(port)
        return {
            "port": port,
            "admin_status": stats.get("ifAdminStatus"),
            "oper_status": stats.get("ifOperStatus"),
            "speed": stats.get("ifSpeed"),
            "name": stats.get("name"),
        }
