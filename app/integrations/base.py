"""
Base classes for billing integrations.

Integrations are code-based (like plugins) - each integration type is implemented as a class.
The database only stores instance configuration (API keys, settings).
"""
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional


class BaseIntegration(ABC):
    """
    Base class for billing integrations.
    
    Each integration type (WHMCS, custom, etc.) should inherit from this
    and implement any custom logic needed.
    """
    
    def __init__(self, integration_type: str):
        self.integration_type = integration_type
    
    @abstractmethod
    def validate_config(self, config: Dict[str, Any]) -> tuple[bool, Optional[str]]:
        """
        Validate integration-specific configuration.
        
        Args:
            config: Configuration dictionary to validate
            
        Returns:
            Tuple of (is_valid, error_message)
            If valid, error_message should be None
        """
        pass
    
    def get_config_schema(self) -> Dict[str, Any]:
        """
        Return JSON schema for this integration's configuration.
        Used by UI to generate configuration forms.
        
        Returns:
            JSON schema dict (optional - only needed if integration has custom config)
        """
        return {}
    
    def get_name(self) -> str:
        """Return human-readable name for this integration type"""
        return self.integration_type.title()
    
    def get_description(self) -> str:
        """Return description of this integration type"""
        return f"{self.get_name()} billing integration"


class GenericIntegration(BaseIntegration):
    """
    Generic integration for standard REST API integrations.
    Most integrations can use this - only override if you need custom logic.
    """
    
    def __init__(self):
        super().__init__("custom")
    
    def validate_config(self, config: Dict[str, Any]) -> tuple[bool, Optional[str]]:
        """Generic integration accepts any config"""
        return True, None
    
    def get_name(self) -> str:
        return "Generic/Custom"
    
    def get_description(self) -> str:
        return "Generic REST API integration for custom billing systems"
