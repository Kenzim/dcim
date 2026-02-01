"""Cable Run model - device-agnostic connection between two ports (server or switch)."""
from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    ForeignKey,
    CheckConstraint,
    DateTime,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.core.database import Base


class CableRun(Base):
    """
    Cable Run - represents a physical cable between two ports.
    Each end is either a switch port or a server port (device-agnostic).
    Exactly one of (end_a_switch_port_id, end_a_server_port_id) is set;
    exactly one of (end_b_switch_port_id, end_b_server_port_id) is set.
    """
    __tablename__ = "cable_runs"

    id = Column(Integer, primary_key=True, index=True)
    end_a_switch_port_id = Column(
        Integer,
        ForeignKey("switch_ports.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        unique=True,
    )
    end_a_server_port_id = Column(
        Integer,
        ForeignKey("network_ports.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        unique=True,
    )
    end_b_switch_port_id = Column(
        Integer,
        ForeignKey("switch_ports.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        unique=True,
    )
    end_b_server_port_id = Column(
        Integer,
        ForeignKey("network_ports.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        unique=True,
    )
    cable_type = Column(String(50), nullable=True)
    speed_mbps = Column(Integer, nullable=True)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    end_a_switch_port = relationship("SwitchPort", foreign_keys=[end_a_switch_port_id])
    end_a_server_port = relationship("NetworkPort", foreign_keys=[end_a_server_port_id])
    end_b_switch_port = relationship("SwitchPort", foreign_keys=[end_b_switch_port_id])
    end_b_server_port = relationship("NetworkPort", foreign_keys=[end_b_server_port_id])

    __table_args__ = (
        CheckConstraint(
            "(end_a_switch_port_id IS NOT NULL AND end_a_server_port_id IS NULL) "
            "OR (end_a_switch_port_id IS NULL AND end_a_server_port_id IS NOT NULL)",
            name="ck_cable_run_end_a_one",
        ),
        CheckConstraint(
            "(end_b_switch_port_id IS NOT NULL AND end_b_server_port_id IS NULL) "
            "OR (end_b_switch_port_id IS NULL AND end_b_server_port_id IS NOT NULL)",
            name="ck_cable_run_end_b_one",
        ),
    )

    def get_other_end(self, switch_port_id=None, server_port_id=None):
        """Return (type, id) for the end that is not the given port. type is 'switch' or 'server'."""
        if switch_port_id is not None:
            if self.end_a_switch_port_id == switch_port_id:
                if self.end_b_switch_port_id is not None:
                    return ("switch", self.end_b_switch_port_id)
                return ("server", self.end_b_server_port_id)
            if self.end_b_switch_port_id == switch_port_id:
                if self.end_a_switch_port_id is not None:
                    return ("switch", self.end_a_switch_port_id)
                return ("server", self.end_a_server_port_id)
        if server_port_id is not None:
            if self.end_a_server_port_id == server_port_id:
                if self.end_b_switch_port_id is not None:
                    return ("switch", self.end_b_switch_port_id)
                return ("server", self.end_b_server_port_id)
            if self.end_b_server_port_id == server_port_id:
                if self.end_a_switch_port_id is not None:
                    return ("switch", self.end_a_switch_port_id)
                return ("server", self.end_a_server_port_id)
        return None

    def __repr__(self):
        a = f"switch:{self.end_a_switch_port_id}" if self.end_a_switch_port_id else f"server:{self.end_a_server_port_id}"
        b = f"switch:{self.end_b_switch_port_id}" if self.end_b_switch_port_id else f"server:{self.end_b_server_port_id}"
        return f"<CableRun(id={self.id}, {a} <-> {b})>"
