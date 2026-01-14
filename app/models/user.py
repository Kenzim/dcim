from sqlalchemy import Column, Integer, String, Boolean
import bcrypt
from app.core.database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(255), unique=True, index=True, nullable=False)
    email = Column(String(255), unique=True, index=True, nullable=False)
    password = Column(String(255), nullable=False)
    is_admin = Column(Boolean, default=False, nullable=False)

    def set_password(self, password: str) -> None:
        """Hash and set the password"""
        # Bcrypt has a 72-byte limit, reject passwords that are too long
        password_bytes = password.encode('utf-8')
        if len(password_bytes) > 72:
            raise ValueError("Password cannot be longer than 72 bytes")
        self.password = bcrypt.hashpw(password_bytes, bcrypt.gensalt()).decode('utf-8')

    def verify_password(self, password: str) -> bool:
        """Verify a password against the stored hash"""
        # Bcrypt has a 72-byte limit, reject passwords that are too long
        password_bytes = password.encode('utf-8')
        if len(password_bytes) > 72:
            return False
        try:
            return bcrypt.checkpw(password_bytes, self.password.encode('utf-8'))
        except Exception:
            return False

