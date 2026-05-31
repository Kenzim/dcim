from sqlalchemy import Column, Integer, ForeignKey, DateTime, UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.core.database import Base


class UserExternalIdentityLink(Base):
    __tablename__ = "user_external_identity_links"
    __table_args__ = (
        UniqueConstraint("user_id", "external_user_id", name="uq_user_external_identity_link_pair"),
        UniqueConstraint("external_user_id", name="uq_user_external_identity_external_unique"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    external_user_id = Column(Integer, ForeignKey("external_users.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    user = relationship("User", backref="external_identity_links")
    external_user = relationship("ExternalUser", backref="user_links")
