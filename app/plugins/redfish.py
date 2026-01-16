"""
Redfish plugin for server management via Redfish API.

Redfish is a standard RESTful API for server management.
This plugin supports power control operations.
"""
import httpx
import logging
from typing import Dict, Optional, Any
from app.plugins.base import (
    ServerPlugin,
    PluginCategory,
    PowerState
)

logger = logging.getLogger(__name__)


class RedfishPlugin(ServerPlugin):
    """
    Redfish plugin for server power management.
    
    Supports power control operations via Redfish REST API.
    """
    
    PLUGIN_NAME = "redfish"
    PLUGIN_VERSION = "1.0.0"
    SUPPORTED_CATEGORIES = [
        PluginCategory.POWER_CONTROL,
    ]
    CONFIG_TEMPLATE = {
        "type": "object",
        "properties": {
            "hostname": {
                "type": "string",
                "title": "Hostname/IP",
                "description": "BMC hostname or IP address",
                "required": True
            },
            "username": {
                "type": "string",
                "title": "Username",
                "description": "Redfish/BMC username",
                "required": True
            },
            "password": {
                "type": "string",
                "title": "Password",
                "description": "Redfish/BMC password",
                "format": "password",
                "required": True
            },
            "port": {
                "type": "integer",
                "title": "Port",
                "description": "Redfish port (default: 443 for HTTPS)",
                "default": 443,
                "required": False
            },
            "use_https": {
                "type": "boolean",
                "title": "Use HTTPS",
                "description": "Use HTTPS for Redfish connections",
                "default": True,
                "required": False
            },
            "verify_ssl": {
                "type": "boolean",
                "title": "Verify SSL",
                "description": "Verify SSL certificates (check to enable SSL verification, uncheck to disable like curl -k)",
                "default": False,
                "required": False
            }
        },
        "required": ["hostname", "username", "password"]
    }
    
    def __init__(self, config: Dict):
        super().__init__(config)
        
        self.hostname = config.get("hostname")
        self.username = config.get("username")
        self.password = config.get("password")
        self.port = config.get("port", 443)
        
        # Handle boolean values - convert strings/None to proper booleans
        use_https_val = config.get("use_https")
        if "use_https" not in config:
            # Key not present - use default
            self.use_https = True
        elif use_https_val is None:
            # Key present but None - treat as False
            self.use_https = False
        elif isinstance(use_https_val, bool):
            self.use_https = use_https_val
        elif isinstance(use_https_val, str):
            self.use_https = use_https_val.lower() in ("true", "1", "yes", "on")
        else:
            self.use_https = bool(use_https_val)
        
        verify_ssl_val = config.get("verify_ssl")
        if "verify_ssl" not in config:
            # Key not present - use default (False, like curl -k)
            self.verify_ssl = False
        elif verify_ssl_val is None:
            # Key present but None - treat as False
            self.verify_ssl = False
        elif isinstance(verify_ssl_val, bool):
            # Explicit boolean value - use it directly
            self.verify_ssl = verify_ssl_val
        elif isinstance(verify_ssl_val, str):
            # String value - convert
            self.verify_ssl = verify_ssl_val.lower() in ("true", "1", "yes", "on")
        else:
            # Other types - convert to bool
            self.verify_ssl = bool(verify_ssl_val)
        
        # Base URL for Redfish API
        protocol = "https" if self.use_https else "http"
        self.base_url = f"{protocol}://{self.hostname}:{self.port}"
        
        # Cache for system ID (lazy loaded)
        self._system_id: Optional[str] = None
        self._system_url: Optional[str] = None
    
    def _get_client(self) -> httpx.AsyncClient:
        """Get an httpx async client with proper SSL settings"""
        return httpx.AsyncClient(
            auth=(self.username, self.password),
            verify=self.verify_ssl,
            timeout=10.0
        )
    
    async def _get_system_id(self) -> str:
        """
        Get the system ID from Redfish.
        Tries /Systems/Self first, then falls back to Systems collection.
        """
        if self._system_id:
            return self._system_id
        
        async with self._get_client() as client:
            # Try /Systems/Self first (common endpoint)
            systems_self_url = f"{self.base_url}/redfish/v1/Systems/Self"
            try:
                response = await client.get(systems_self_url)
                response.raise_for_status()
                self._system_url = systems_self_url
                self._system_id = "Self"
                return self._system_id
            except httpx.HTTPStatusError:
                # If Self doesn't exist (404), try Systems collection
                pass
            except httpx.HTTPError:
                # Other HTTP errors, try Systems collection
                pass
            
            # Fall back to Systems collection
            try:
                systems_url = f"{self.base_url}/redfish/v1/Systems"
                response = await client.get(systems_url)
                response.raise_for_status()
                
                data = response.json()
                members = data.get("Members", [])
                
                if not members:
                    raise Exception("No systems found in Redfish")
                
                # Get first system (most common case)
                system_odata_id = members[0].get("@odata.id")
                if not system_odata_id:
                    raise Exception("Invalid system member format")
                
                # Extract system ID from URL (e.g., /redfish/v1/Systems/1 -> "1")
                self._system_url = f"{self.base_url}{system_odata_id}"
                self._system_id = system_odata_id.split("/")[-1]
                
                return self._system_id
            except httpx.HTTPError as e:
                raise Exception(f"Failed to connect to Redfish: {str(e)}")
    
    async def _get_system_url(self) -> str:
        """Get the full URL to the system resource"""
        if not self._system_url:
            await self._get_system_id()
        return self._system_url
    
    async def test_connection(self) -> Dict[str, Any]:
        """
        Test the Redfish connection and verify credentials.
        
        Attempts to:
        1. Try /Systems/Self first (common endpoint, requires auth)
        2. Fall back to /Systems collection if Self doesn't exist
        3. Get system information to verify full access
        
        Returns:
            Dict with success status, message, and details
        """
        try:
            logger.info(f"Testing Redfish connection to {self.base_url}")
            async with self._get_client() as client:
                system_info = None
                system_url = None
                
                # Test 1: Try /Systems/Self first (common endpoint, directly requires auth)
                # This is what many Redfish implementations use
                systems_self_url = f"{self.base_url}/redfish/v1/Systems/Self"
                logger.debug(f"Attempting to connect to {systems_self_url}")
                try:
                    self_response = await client.get(systems_self_url)
                    logger.debug(f"Response status: {self_response.status_code}")
                    self_response.raise_for_status()
                    system_info = self_response.json()
                    system_url = systems_self_url
                    self._system_url = systems_self_url
                    self._system_id = "Self"
                    logger.info(f"Successfully connected via /Systems/Self")
                except httpx.HTTPStatusError as e:
                    logger.warning(f"HTTP error {e.response.status_code} on /Systems/Self: {e.response.text[:200]}")
                    if e.response.status_code == 401:
                        logger.error(f"Authentication failed: Invalid credentials for {self.username}@{self.hostname}")
                        # Auth failed, re-raise to be caught below
                        raise
                    # If 404, try Systems collection instead
                    if e.response.status_code == 404:
                        logger.debug("/Systems/Self not found, trying Systems collection")
                        pass  # Will try Systems collection below
                    else:
                        raise
                except Exception as e:
                    logger.warning(f"Exception accessing /Systems/Self: {type(e).__name__}: {str(e)}")
                    # Other error, try Systems collection
                    pass
                
                # Test 2: If Self didn't work, try Systems collection
                if system_info is None:
                    systems_url = f"{self.base_url}/redfish/v1/Systems"
                    logger.debug(f"Attempting to connect to {systems_url}")
                    systems_response = await client.get(systems_url)
                    logger.debug(f"Response status: {systems_response.status_code}")
                    systems_response.raise_for_status()  # Will raise HTTPStatusError for 401
                    
                    systems_data = systems_response.json()
                    members = systems_data.get("Members", [])
                    member_count = len(members)
                    logger.info(f"Found {member_count} system(s) in Systems collection")
                    
                    if member_count == 0:
                        logger.warning("No systems found in Redfish Systems collection")
                        return {
                            "success": False,
                            "message": "Authentication successful but no systems found in Redfish",
                            "details": {"systems_found": 0}
                        }
                    
                    # Get first system
                    system_odata_id = members[0].get("@odata.id")
                    if system_odata_id:
                        system_url = f"{self.base_url}{system_odata_id}"
                        logger.debug(f"Fetching system details from {system_url}")
                        system_response = await client.get(system_url)
                        system_response.raise_for_status()
                        system_info = system_response.json()
                        self._system_url = system_url
                        self._system_id = system_odata_id.split("/")[-1]
                        logger.info(f"Successfully retrieved system details for system ID: {self._system_id}")
                
                if system_info is None:
                    return {
                        "success": False,
                        "message": "Could not access system information",
                        "details": {}
                    }
                
                # Test 3: Get service root for version info (after confirming auth works)
                service_root_url = f"{self.base_url}/redfish/v1"
                logger.debug(f"Fetching service root from {service_root_url}")
                try:
                    service_root_response = await client.get(service_root_url)
                    service_root_response.raise_for_status()
                    service_root = service_root_response.json()
                    redfish_version = service_root.get("RedfishVersion", "Unknown")
                    product = service_root.get("Product", "Unknown")
                    logger.info(f"Redfish version: {redfish_version}, Product: {product}")
                except Exception as e:
                    logger.warning(f"Could not fetch service root info: {str(e)}")
                    redfish_version = "Unknown"
                    product = "Unknown"
                
                logger.info(f"Connection test successful for {self.hostname}")
                return {
                    "success": True,
                    "message": f"Successfully connected and authenticated to Redfish API (v{redfish_version})",
                    "details": {
                        "redfish_version": redfish_version,
                        "product": product,
                        "system_id": self._system_id if self._system_id else None,
                        "system_model": system_info.get("Model") if system_info else None,
                        "system_manufacturer": system_info.get("Manufacturer") if system_info else None,
                        "power_state": system_info.get("PowerState") if system_info else None,
                    }
                }
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error {e.response.status_code}: {e.response.text[:500]}")
            logger.error(f"Request URL: {e.request.url}")
            logger.error(f"Request headers: {dict(e.request.headers)}")
            if e.response.status_code == 401:
                logger.error(f"Authentication failed for {self.username}@{self.hostname}:{self.port}")
                return {
                    "success": False,
                    "message": "Authentication failed: Invalid username or password",
                    "details": {
                        "status_code": e.response.status_code,
                        "response_text": e.response.text[:200],
                        "url": str(e.request.url)
                    }
                }
            elif e.response.status_code == 404:
                logger.error(f"Redfish endpoint not found at {self.hostname}")
                return {
                    "success": False,
                    "message": f"Redfish endpoint not found. Is Redfish enabled on {self.hostname}?",
                    "details": {
                        "status_code": e.response.status_code,
                        "url": str(e.request.url)
                    }
                }
            else:
                logger.error(f"Unexpected HTTP error {e.response.status_code}")
                return {
                    "success": False,
                    "message": f"HTTP error {e.response.status_code}: {e.response.text[:200]}",
                    "details": {
                        "status_code": e.response.status_code,
                        "response_text": e.response.text[:200],
                        "url": str(e.request.url)
                    }
                }
        except httpx.ConnectError as e:
            error_str = str(e)
            logger.error(f"Connection error to {self.hostname}:{self.port}: {error_str}")
            logger.error(f"verify_ssl setting: {self.verify_ssl}")
            
            # Check if this is an SSL verification error
            if "CERTIFICATE_VERIFY_FAILED" in error_str or "certificate verify failed" in error_str.lower():
                if not self.verify_ssl:
                    logger.error(f"SSL verification error occurred but verify_ssl={self.verify_ssl}. This should not happen!")
                    return {
                        "success": False,
                        "message": f"SSL verification error: {error_str}. Verify SSL is disabled but error still occurred. Check server logs.",
                        "details": {
                            "hostname": self.hostname,
                            "port": self.port,
                            "verify_ssl": self.verify_ssl,
                            "error": error_str
                        }
                    }
                else:
                    return {
                        "success": False,
                        "message": f"SSL certificate verification failed. Uncheck 'Verify SSL' to disable verification (like curl -k)",
                        "details": {
                            "hostname": self.hostname,
                            "port": self.port,
                            "verify_ssl": self.verify_ssl,
                            "error": error_str
                        }
                    }
            
            return {
                "success": False,
                "message": f"Connection failed: Could not reach {self.hostname}:{self.port}",
                "details": {
                    "hostname": self.hostname,
                    "port": self.port,
                    "error": error_str
                }
            }
        except httpx.TimeoutException as e:
            logger.error(f"Timeout connecting to {self.hostname}:{self.port}: {str(e)}")
            return {
                "success": False,
                "message": f"Connection timeout: {self.hostname}:{self.port} did not respond",
                "details": {
                    "hostname": self.hostname,
                    "port": self.port,
                    "error": str(e)
                }
            }
        except Exception as e:
            logger.exception(f"Unexpected error during connection test: {str(e)}")
            return {
                "success": False,
                "message": f"Connection test failed: {str(e)}",
                "details": {
                    "error_type": type(e).__name__,
                    "error": str(e)
                }
            }
    
    # ========== Power Control Methods ==========
    
    async def get_power_state(self) -> PowerState:
        """
        Get current power state from Redfish API.
        
        GET /redfish/v1/Systems/{SystemId} -> PowerState field
        """
        async with self._get_client() as client:
            system_url = await self._get_system_url()
            
            try:
                response = await client.get(system_url)
                response.raise_for_status()
                
                data = response.json()
                power_state_str = data.get("PowerState", "Unknown")
                
                # Map Redfish power states to our PowerState enum
                power_state_map = {
                    "On": PowerState.ON,
                    "Off": PowerState.OFF,
                    "PoweringOn": PowerState.ON,  # Transitioning to on
                    "PoweringOff": PowerState.OFF,  # Transitioning to off
                }
                
                return power_state_map.get(power_state_str, PowerState.UNKNOWN)
            except httpx.HTTPError as e:
                raise Exception(f"Failed to get power state: {str(e)}")
    
    async def power_on(self) -> bool:
        """
        Power on the server via Redfish.
        
        POST /redfish/v1/Systems/{SystemId}/Actions/ComputerSystem.Reset
        Body: {"ResetType": "On"}
        """
        async with self._get_client() as client:
            system_url = await self._get_system_url()
            reset_url = f"{system_url}/Actions/ComputerSystem.Reset"
            
            try:
                payload = {"ResetType": "On"}
                response = await client.post(reset_url, json=payload)
                response.raise_for_status()
                return True
            except httpx.HTTPError as e:
                raise Exception(f"Failed to power on server: {str(e)}")
    
    async def power_off(self, force: bool = False) -> bool:
        """
        Power off the server via Redfish.
        
        Args:
            force: If True, force power off (ForceOff vs GracefulOff)
        
        POST /redfish/v1/Systems/{SystemId}/Actions/ComputerSystem.Reset
        Body: {"ResetType": "ForceOff" if force else "GracefulOff"}
        """
        async with self._get_client() as client:
            system_url = await self._get_system_url()
            reset_url = f"{system_url}/Actions/ComputerSystem.Reset"
            
            try:
                reset_type = "ForceOff" if force else "GracefulOff"
                payload = {"ResetType": reset_type}
                response = await client.post(reset_url, json=payload)
                response.raise_for_status()
                return True
            except httpx.HTTPError as e:
                raise Exception(f"Failed to power off server: {str(e)}")
    
    async def power_reset(self) -> bool:
        """
        Reset/reboot the server via Redfish.
        
        POST /redfish/v1/Systems/{SystemId}/Actions/ComputerSystem.Reset
        Body: {"ResetType": "GracefulRestart"}
        """
        async with self._get_client() as client:
            system_url = await self._get_system_url()
            reset_url = f"{system_url}/Actions/ComputerSystem.Reset"
            
            try:
                payload = {"ResetType": "GracefulRestart"}
                response = await client.post(reset_url, json=payload)
                response.raise_for_status()
                return True
            except httpx.HTTPError as e:
                raise Exception(f"Failed to reset server: {str(e)}")
    
    # ========== User Account Control Methods ==========
    # Not supported by this plugin
    
    async def list_users(self):
        raise NotImplementedError("Redfish plugin does not support user account control")
    
    async def create_user(self, username: str, password: str, roles=None):
        raise NotImplementedError("Redfish plugin does not support user account control")
    
    async def delete_user(self, username: str):
        raise NotImplementedError("Redfish plugin does not support user account control")
    
    async def update_user_password(self, username: str, new_password: str):
        raise NotImplementedError("Redfish plugin does not support user account control")
    
    # ========== Boot Order Control Methods ==========
    # Not supported by this plugin
    
    async def get_boot_order(self):
        raise NotImplementedError("Redfish plugin does not support boot order control")
    
    async def set_boot_order(self, boot_devices):
        raise NotImplementedError("Redfish plugin does not support boot order control")
    
    async def set_next_boot_device(self, device: str):
        raise NotImplementedError("Redfish plugin does not support boot order control")

