"""
Seed script to populate default categories in the database.
"""
from sqlalchemy.orm import Session
from app.dao import CategoryDAO


# Default categories that match PluginCategory enum
DEFAULT_CATEGORIES = [
    {
        "name": "power_control",
        "display_name": "Power Control",
        "description": "Control server power state (on, off, reset)"
    },
    {
        "name": "user_account_control",
        "display_name": "User Account Control",
        "description": "Manage user accounts on the server"
    },
    {
        "name": "boot_order_control",
        "display_name": "Boot Order Control",
        "description": "Manage boot order and boot device selection"
    }
]


def seed_categories(db: Session) -> None:
    """
    Seed default categories into the database.
    Creates categories if they don't exist.
    """
    for category_data in DEFAULT_CATEGORIES:
        CategoryDAO.get_or_create(
            db,
            name=category_data["name"],
            display_name=category_data["display_name"],
            description=category_data["description"]
        )

