"""
Integration registry - manages billing integration types.

Similar to plugin registry - integration types are registered in code,
instances are stored in the database.
"""
from typing import Dict, Optional, Type
from app.integrations.base import BaseIntegration, GenericIntegration
from app.integrations.whmcs import WHMCSIntegration


class IntegrationRegistry:
    """Registry for billing integration types"""
    
    def __init__(self):
        self._integrations: Dict[str, Type[BaseIntegration]] = {}
        self._register_defaults()
    
    def _register_defaults(self):
        """Register default integration types"""
        # Register implemented integration types
        self.register("whmcs", WHMCSIntegration)
        # Generic integration is always available as fallback
        self.register("custom", GenericIntegration)
    
    def register(self, integration_type: str, integration_class: Type[BaseIntegration]):
        """Register an integration type"""
        if not issubclass(integration_class, BaseIntegration):
            raise ValueError(f"Integration class must inherit from BaseIntegration")
        self._integrations[integration_type] = integration_class
    
    def get(self, integration_type: str) -> Optional[Type[BaseIntegration]]:
        """Get integration class by type"""
        return self._integrations.get(integration_type)
    
    def get_all(self) -> Dict[str, Type[BaseIntegration]]:
        """Get all registered integration types"""
        return self._integrations.copy()
    
    def create_instance(self, integration_type: str) -> Optional[BaseIntegration]:
        """Create an instance of an integration"""
        integration_class = self.get(integration_type)
        if integration_class:
            return integration_class()
        return None
    
    def is_registered(self, integration_type: str) -> bool:
        """Check if an integration type is registered"""
        return integration_type in self._integrations


# Singleton instance
_integration_registry: Optional[IntegrationRegistry] = None


def get_integration_registry() -> IntegrationRegistry:
    """Get the singleton integration registry instance"""
    global _integration_registry
    if _integration_registry is None:
        _integration_registry = IntegrationRegistry()
    return _integration_registry
