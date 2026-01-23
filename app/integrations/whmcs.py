"""
WHMCS billing integration.

This integration type provides WHMCS-specific functionality and validation.
"""
from typing import Dict, Any, Optional
from app.integrations.base import BaseIntegration


class WHMCSIntegration(BaseIntegration):
    """WHMCS billing integration"""
    
    def __init__(self):
        super().__init__("whmcs")
    
    def validate_config(self, config: Dict[str, Any]) -> tuple[bool, Optional[str]]:
        """
        Validate WHMCS-specific configuration.
        
        Expected config:
        - api_url: str (optional) - WHMCS API endpoint URL
        - api_identifier: str (optional) - WHMCS API identifier
        - api_secret: str (optional) - WHMCS API secret
        
        For now, config is optional as WHMCS will be calling our API, not the other way around.
        """
        # Config is optional - WHMCS calls our API, we don't call theirs
        return True, None
    
    def get_config_schema(self) -> Dict[str, Any]:
        """Return JSON schema for WHMCS configuration"""
        return {
            "type": "object",
            "properties": {
                "api_url": {
                    "type": "string",
                    "description": "WHMCS API endpoint URL (optional - for future use)"
                },
                "api_identifier": {
                    "type": "string",
                    "description": "WHMCS API identifier (optional - for future use)"
                },
                "api_secret": {
                    "type": "string",
                    "description": "WHMCS API secret (optional - for future use)"
                }
            },
            "additionalProperties": True
        }
    
    def get_name(self) -> str:
        return "WHMCS"
    
    def get_description(self) -> str:
        return "WHMCS billing system integration. WHMCS will call our API to manage services."
