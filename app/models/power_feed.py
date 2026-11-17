"""Power feed model: a circuit supplied to a rack (for example a 208 V 30 A whip)."""
from sqlalchemy import Column, Integer, String, Text, ForeignKey, DateTime, UniqueConstraint
from sqlalchemy.orm import relationship, backref
from sqlalchemy.sql import func
from app.core.database import Base


class PowerFeed(Base):
    """A power feed entering a rack. Entered by hand; PDU inlets are linked to it."""
    __tablename__ = "power_feeds"

    id = Column(Integer, primary_key=True, index=True)
    rack_id = Column(Integer, ForeignKey("racks.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(64), nullable=False)
    voltage = Column(Integer, nullable=False, default=208)
    amperage = Column(Integer, nullable=False, default=30)  # Breaker rating in amps
    phase = Column(String(16), nullable=False, default="single")  # "single" or "three"
    max_utilisation_percent = Column(Integer, nullable=False, default=80)  # Continuous-load derating
    description = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    rack = relationship(
        "Rack",
        backref=backref("power_feeds", cascade="all, delete-orphan", passive_deletes=True, order_by="PowerFeed.name"),
    )

    __table_args__ = (UniqueConstraint("rack_id", "name", name="uq_power_feeds_rack_name"),)

    def __repr__(self):
        return f"<PowerFeed(id={self.id}, rack_id={self.rack_id}, name='{self.name}', {self.voltage}V {self.amperage}A)>"
