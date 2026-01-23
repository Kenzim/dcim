"""
Billing integrations module.

Integration types are code-based (like plugins) - each type is a class.
Database stores instance configuration only.
"""
from app.integrations.base import BaseIntegration, GenericIntegration
from app.integrations.registry import IntegrationRegistry, get_integration_registry
from app.integrations.whmcs import WHMCSIntegration

__all__ = [
    "BaseIntegration",
    "GenericIntegration",
    "WHMCSIntegration",
    "IntegrationRegistry",
    "get_integration_registry",
]
