from sqlalchemy.orm import Session
from typing import Optional, List
from app.models.script import Script


class ScriptDAO:
    """Data Access Object for Script model"""

    @staticmethod
    def create(
        db: Session,
        name: str,
        content: str,
        description: Optional[str] = None,
        enabled: bool = True,
        user_executable: bool = False
    ) -> Script:
        """Create a new script"""
        script = Script(
            name=name,
            content=content,
            description=description,
            enabled=enabled,
            user_executable=user_executable
        )
        db.add(script)
        db.commit()
        db.refresh(script)
        return script

    @staticmethod
    def get_by_id(db: Session, script_id: int) -> Optional[Script]:
        """Get script by ID"""
        return db.query(Script).filter(Script.id == script_id).first()

    @staticmethod
    def get_by_name(db: Session, name: str) -> Optional[Script]:
        """Get script by name"""
        return db.query(Script).filter(Script.name == name).first()

    @staticmethod
    def get_all(db: Session, enabled_only: bool = False, user_executable_only: bool = False) -> List[Script]:
        """Get all scripts with optional filters"""
        query = db.query(Script)
        if enabled_only:
            query = query.filter(Script.enabled == True)
        if user_executable_only:
            query = query.filter(Script.user_executable == True)
        return query.order_by(Script.name).all()

    @staticmethod
    def update(db: Session, script: Script) -> Script:
        """Update a script"""
        db.commit()
        db.refresh(script)
        return script

    @staticmethod
    def delete(db: Session, script_id: int) -> bool:
        """Delete a script by ID"""
        script = db.query(Script).filter(Script.id == script_id).first()
        if script:
            db.delete(script)
            db.commit()
            return True
        return False
