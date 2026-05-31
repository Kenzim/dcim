from sqlalchemy import Column, Integer, DateTime, JSON, ForeignKey, Boolean
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.core.database import Base


class FamilyVMConfig(Base):
    __tablename__ = "family_vm_configs"

    id = Column(Integer, primary_key=True, index=True)
    family_id = Column(Integer, ForeignKey("product_families.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    config = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    family = relationship("ProductFamily", backref="vm_config_row")


class ProductVMConfig(Base):
    __tablename__ = "product_vm_configs"

    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    extends_family = Column(Boolean, nullable=False, default=True)
    config = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    product = relationship("Product", backref="vm_config_row")
