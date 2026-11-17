from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.core.database import Base


class SwitchLldpNeighbor(Base):
    """One LLDP neighbour seen on a switch port at the last collection (snapshot, replaced per switch)."""

    __tablename__ = "switch_lldp_neighbors"

    id = Column(Integer, primary_key=True, index=True)
    switch_id = Column(Integer, ForeignKey("network_switches.id", ondelete="CASCADE"), nullable=False, index=True)
    switch_port_id = Column(Integer, ForeignKey("switch_ports.id", ondelete="SET NULL"), nullable=True, index=True)
    local_port_num = Column(Integer, nullable=True)  # lldpRemLocalPortNum
    local_port_label = Column(String(255), nullable=True)  # lldpLocPortId or lldpLocPortDesc as reported
    chassis_id_subtype = Column(Integer, nullable=True)
    chassis_id = Column(String(255), nullable=True, index=True)
    port_id_subtype = Column(Integer, nullable=True)
    port_id = Column(String(255), nullable=True, index=True)
    port_desc = Column(String(255), nullable=True)
    sys_name = Column(String(255), nullable=True)
    sys_desc = Column(Text, nullable=True)
    collected_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    switch = relationship("NetworkSwitch")
    switch_port = relationship("SwitchPort")
