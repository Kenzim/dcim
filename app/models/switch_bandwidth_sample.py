"""Switch bandwidth sample model for storing SNMP port counter snapshots."""
from sqlalchemy import Column, Integer, BigInteger, String, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.core.database import Base


class SwitchBandwidthSample(Base):
    """
    One snapshot of IF-MIB octet counters (and errors/discards) for a switch port.

    Stores cumulative counters (ifInOctets, ifOutOctets, etc.) at a point in time.
    Bandwidth rate can be computed as (delta bytes) / (delta time) between two samples.
    """
    __tablename__ = "switch_bandwidth_samples"

    id = Column(Integer, primary_key=True, index=True)
    switch_id = Column(
        Integer,
        ForeignKey("network_switches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    port_identifier = Column(String(255), nullable=False, index=True)  # Port name or "ifIndex-<n>"
    if_index = Column(Integer, nullable=True)
    sampled_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    bytes_in = Column(BigInteger, nullable=False, default=0)
    bytes_out = Column(BigInteger, nullable=False, default=0)
    in_errors = Column(BigInteger, nullable=False, default=0)
    out_errors = Column(BigInteger, nullable=False, default=0)
    in_discards = Column(BigInteger, nullable=False, default=0)
    out_discards = Column(BigInteger, nullable=False, default=0)

    switch = relationship("NetworkSwitch", backref="bandwidth_samples")

    def __repr__(self):
        return (
            f"<SwitchBandwidthSample(switch_id={self.switch_id}, port='{self.port_identifier}', "
            f"at={self.sampled_at}, in={self.bytes_in}, out={self.bytes_out})>"
        )
