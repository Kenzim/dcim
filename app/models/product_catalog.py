from sqlalchemy import Column, Integer, String, DateTime, Boolean, JSON, ForeignKey, UniqueConstraint, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.core.database import Base


class ProductFamily(Base):
    __tablename__ = "product_families"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    code = Column(String(128), nullable=False, unique=True, index=True)
    service_type = Column(String(32), nullable=False, index=True)
    provisioning_backend = Column(String(64), nullable=True, index=True)
    defaults = Column(JSON, nullable=False, default=dict)
    constraints = Column(JSON, nullable=False, default=dict)
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    products = relationship("Product", back_populates="family", cascade="all, delete-orphan")
    os_mappings = relationship("ProductFamilyOSProfile", back_populates="family", cascade="all, delete-orphan")


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("family_id", "code", name="uq_products_family_code"),)

    id = Column(Integer, primary_key=True, index=True)
    family_id = Column(Integer, ForeignKey("product_families.id", ondelete="SET NULL"), nullable=True, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    code = Column(String(128), nullable=False, unique=True, index=True)
    overrides = Column(JSON, nullable=False, default=dict)
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    family = relationship("ProductFamily", back_populates="products")
    vm_template_mappings = relationship("ProductVMTemplate", back_populates="product", cascade="all, delete-orphan")


class OSProfile(Base):
    __tablename__ = "os_profiles"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(128), nullable=False, unique=True, index=True)
    name = Column(String(255), nullable=False)
    os_family = Column(String(64), nullable=False, index=True)
    strategy_name = Column(String(128), nullable=True, index=True)
    strategy_config = Column(JSON, nullable=False, default=dict)
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    family_mappings = relationship("ProductFamilyOSProfile", back_populates="os_profile", cascade="all, delete-orphan")


class VMTemplate(Base):
    __tablename__ = "vm_templates"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    os_type = Column(String(128), nullable=False, index=True)
    proxmox_template_name = Column(String(255), nullable=False, unique=True, index=True)
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    product_mappings = relationship("ProductVMTemplate", back_populates="vm_template", cascade="all, delete-orphan")


class ProductFamilyOSProfile(Base):
    __tablename__ = "product_family_os_profiles"
    __table_args__ = (UniqueConstraint("family_id", "os_profile_id", name="uq_family_os_profile"),)

    id = Column(Integer, primary_key=True, index=True)
    family_id = Column(Integer, ForeignKey("product_families.id", ondelete="CASCADE"), nullable=False, index=True)
    os_profile_id = Column(Integer, ForeignKey("os_profiles.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    family = relationship("ProductFamily", back_populates="os_mappings")
    os_profile = relationship("OSProfile", back_populates="family_mappings")


class ProductVMTemplate(Base):
    __tablename__ = "product_vm_templates"
    __table_args__ = (UniqueConstraint("product_id", "vm_template_id", name="uq_product_vm_template"),)

    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    vm_template_id = Column(Integer, ForeignKey("vm_templates.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    product = relationship("Product", back_populates="vm_template_mappings")
    vm_template = relationship("VMTemplate", back_populates="product_mappings")
