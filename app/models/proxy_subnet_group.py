"""Named pools of IPAM subnets used by proxy catalog products."""
from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.core.database import Base


class ProxySubnetGroup(Base):
    __tablename__ = "proxy_subnet_groups"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    name = Column(String(255), nullable=False)
    code = Column(String(128), nullable=False, unique=True, index=True)
    description = Column(Text, nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    members = relationship(
        "ProxySubnetGroupMember",
        back_populates="group",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class ProxySubnetGroupMember(Base):
    __tablename__ = "proxy_subnet_group_members"
    __table_args__ = (UniqueConstraint("group_id", "subnet_id", name="uq_proxy_subnet_group_member"),)

    group_id = Column(
        Integer,
        ForeignKey("proxy_subnet_groups.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    subnet_id = Column(
        Integer,
        ForeignKey("ip_subnets.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
        index=True,
    )

    group = relationship("ProxySubnetGroup", back_populates="members")
    subnet = relationship("IPSubnet")
