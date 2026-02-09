"""
Plugin sync functions - now no-ops since plugins are not stored in the database.
These functions exist for backward compatibility.
"""
from sqlalchemy.orm import Session
from typing import Dict, Any
import logging

logger = logging.getLogger(__name__)


def sync_plugins_to_db(db: Session) -> Dict[str, Any]:
    """
    No-op: Server plugins are no longer stored in the database.
    They are loaded directly from disk via the registry.
    
    Returns:
        Dictionary with empty sync results
    """
    logger.info("Plugin sync called but plugins are no longer stored in database")
    return {
        "created": [],
        "updated": [],
        "errors": []
    }


def sync_switch_plugins_to_db(db: Session) -> Dict[str, Any]:
    """
    No-op: Switch plugins are no longer stored in the database.
    They are loaded directly from disk via the switch registry.
    
    Returns:
        Dictionary with empty sync results
    """
    logger.info("Switch plugin sync called but plugins are no longer stored in database")
    return {
        "created": [],
        "updated": [],
        "errors": []
    }

