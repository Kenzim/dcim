#!/usr/bin/env python3
"""Script to create an admin user"""
import sys
import os

# Add the project root to the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.database import SessionLocal
from app.dao.user_dao import UserDAO

def create_admin_user(username: str, email: str, password: str):
    """Create an admin user"""
    db = SessionLocal()
    try:
        # Check if user already exists
        existing_user = UserDAO.get_by_username(db, username)
        if existing_user:
            print(f"Error: User '{username}' already exists!", file=sys.stderr)
            sys.exit(1)
        
        existing_email = UserDAO.get_by_email(db, email)
        if existing_email:
            print(f"Error: Email '{email}' is already in use!", file=sys.stderr)
            sys.exit(1)
        
        # Create admin user
        user = UserDAO.create(db, username=username, email=email, password=password, is_admin=True)
        print(f"Successfully created admin user:")
        print(f"  Username: {user.username}")
        print(f"  Email: {user.email}")
        print(f"  Admin: {user.is_admin}")
        return user
    except Exception as e:
        print(f"Error creating admin user: {e}", file=sys.stderr)
        db.rollback()
        sys.exit(1)
    finally:
        db.close()

if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Usage: python3 scripts/create_admin_user.py <username> <email> <password>", file=sys.stderr)
        print("Example: python3 scripts/create_admin_user.py admin admin@example.com mypassword123", file=sys.stderr)
        sys.exit(1)
    
    username = sys.argv[1]
    email = sys.argv[2]
    password = sys.argv[3]
    
    create_admin_user(username, email, password)
