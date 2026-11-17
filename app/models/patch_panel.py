"""Patch panel model: a passive panel whose ports join a front cable to a rear cable."""
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.core.database import Base

PATCH_PANEL_MEDIA = ("copper", "fibre")
PATCH_SIDES = ("front", "rear")


class PatchPanel(Base):
    __tablename__ = "patch_panels"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    location_id = Column(Integer, ForeignKey("locations.id"), nullable=False, index=True)
    rack_id = Column(Integer, ForeignKey("racks.id", ondelete="SET NULL"), nullable=True, index=True)
    rack_unit = Column(Integer, nullable=True)
    rack_units = Column(Integer, nullable=False, default=1, server_default="1")
    port_count = Column(Integer, nullable=False)
    medium = Column(String(20), nullable=False, default="copper", server_default="copper")
    description = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    location = relationship("Location")
    rack = relationship("Rack")
    ports = relationship(
        "PatchPanelPort",
        back_populates="patch_panel",
        cascade="all, delete-orphan",
        order_by="PatchPanelPort.number",
    )

    def __repr__(self):
        return f"<PatchPanel(id={self.id}, name='{self.name}', ports={self.port_count})>"


class PatchPanelPort(Base):
    __tablename__ = "patch_panel_ports"

    id = Column(Integer, primary_key=True, index=True)
    patch_panel_id = Column(Integer, ForeignKey("patch_panels.id", ondelete="CASCADE"), nullable=False, index=True)
    number = Column(Integer, nullable=False)
    label = Column(String(255), nullable=True)

    patch_panel = relationship("PatchPanel", back_populates="ports")

    __table_args__ = (UniqueConstraint("patch_panel_id", "number", name="uq_patch_panel_port_number"),)

    @property
    def name(self) -> str:
        return self.label or f"Port {self.number}"

    def __repr__(self):
        return f"<PatchPanelPort(id={self.id}, panel={self.patch_panel_id}, number={self.number})>"
