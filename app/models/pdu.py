"""PDU models: the PDU itself, its inlets (power inputs) and its outlets."""
from sqlalchemy import (
    Column,
    Integer,
    String,
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Text,
    Float,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.core.database import Base


class Pdu(Base):
    """
    Power distribution unit. Linked to a PDU plugin (plugin_name + plugin_config) like a switch.

    rack_units = 0 means a zero-U (vertical) PDU that does not occupy rack units.
    last_readings holds the latest whole-unit readings from the plugin (totals and banks);
    per-inlet and per-outlet readings live on PduInlet / PduOutlet.
    """
    __tablename__ = "pdus"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), unique=True, index=True, nullable=False)
    description = Column(Text, nullable=True)
    location_id = Column(Integer, ForeignKey("locations.id"), nullable=False, index=True)
    rack_id = Column(Integer, ForeignKey("racks.id"), nullable=True, index=True)
    rack_unit = Column(Integer, nullable=True)  # Bottom U occupied; NULL for zero-U or unplaced
    rack_units = Column(Integer, nullable=False, default=0)
    plugin_name = Column(String(255), nullable=False, index=True)
    plugin_config = Column(JSON, nullable=False)
    enabled = Column(Boolean, default=True, nullable=False)
    model = Column(String(255), nullable=True)
    serial_number = Column(String(255), nullable=True)
    firmware_version = Column(String(255), nullable=True)
    inlet_count = Column(Integer, nullable=False, default=1)
    outlet_count = Column(Integer, nullable=False, default=0)
    # What the hardware can do, as reported by the plugin (or set by hand)
    outlet_metering = Column(Boolean, nullable=False, default=False)
    outlet_switching = Column(Boolean, nullable=False, default=False)
    # Admin opt-in before Rackflow may switch outlets on this PDU
    outlet_control_enabled = Column(Boolean, nullable=False, default=False)
    last_readings = Column(JSON, nullable=True)
    last_polled_at = Column(DateTime(timezone=True), nullable=True)
    last_poll_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    location = relationship("Location", backref="pdus")
    rack = relationship("Rack", backref="pdus")
    inlets = relationship(
        "PduInlet", back_populates="pdu", cascade="all, delete-orphan", order_by="PduInlet.position"
    )
    outlets = relationship(
        "PduOutlet", back_populates="pdu", cascade="all, delete-orphan", order_by="PduOutlet.position"
    )

    def __repr__(self):
        return f"<Pdu(id={self.id}, name='{self.name}', plugin_name='{self.plugin_name}')>"


class PduInlet(Base):
    """A power input on a PDU (one on most PDUs, two on an ATS). Linked to a rack power feed."""
    __tablename__ = "pdu_inlets"

    id = Column(Integer, primary_key=True, index=True)
    pdu_id = Column(Integer, ForeignKey("pdus.id", ondelete="CASCADE"), nullable=False, index=True)
    position = Column(Integer, nullable=False)  # 1-based input number on the PDU
    name = Column(String(64), nullable=False)
    power_feed_id = Column(Integer, ForeignKey("power_feeds.id", ondelete="SET NULL"), nullable=True, index=True)
    voltage_v = Column(Float, nullable=True)
    current_a = Column(Float, nullable=True)
    power_w = Column(Float, nullable=True)
    apparent_power_va = Column(Float, nullable=True)
    energy_kwh = Column(Float, nullable=True)
    active = Column(Boolean, nullable=True)  # ATS: True for the input carrying the load; NULL if not known

    pdu = relationship("Pdu", back_populates="inlets")
    power_feed = relationship("PowerFeed", backref="pdu_inlets")

    __table_args__ = (UniqueConstraint("pdu_id", "position", name="uq_pdu_inlets_pdu_position"),)

    def __repr__(self):
        return f"<PduInlet(id={self.id}, pdu_id={self.pdu_id}, position={self.position}, feed={self.power_feed_id})>"


class PduOutlet(Base):
    """An outlet on a PDU. Server power inputs link to it."""
    __tablename__ = "pdu_outlets"

    id = Column(Integer, primary_key=True, index=True)
    pdu_id = Column(Integer, ForeignKey("pdus.id", ondelete="CASCADE"), nullable=False, index=True)
    position = Column(Integer, nullable=False)  # 1-based outlet number on the PDU
    name = Column(String(64), nullable=False)
    bank = Column(String(64), nullable=True)  # Branch / bank / breaker group the outlet sits on
    description = Column(Text, nullable=True)
    current_a = Column(Float, nullable=True)
    power_w = Column(Float, nullable=True)
    state = Column(String(16), nullable=True)  # "on", "off" or NULL when the PDU does not report it
    locked = Column(Boolean, nullable=False, default=False)  # True blocks switching this outlet from Rackflow

    pdu = relationship("Pdu", back_populates="outlets")

    __table_args__ = (UniqueConstraint("pdu_id", "position", name="uq_pdu_outlets_pdu_position"),)

    def __repr__(self):
        return f"<PduOutlet(id={self.id}, pdu_id={self.pdu_id}, position={self.position})>"
