"""Cable Run model - device-agnostic connection between two ports (server, switch or patch panel)."""
from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    ForeignKey,
    CheckConstraint,
    DateTime,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.core.database import Base


def _one_end_sql(which: str) -> str:
    sw, sv, pp, side = (f"end_{which}_{col}" for col in ("switch_port_id", "server_port_id", "patch_port_id", "patch_side"))
    return (
        f"(({sw} IS NOT NULL AND {sv} IS NULL AND {pp} IS NULL) "
        f"OR ({sw} IS NULL AND {sv} IS NOT NULL AND {pp} IS NULL) "
        f"OR ({sw} IS NULL AND {sv} IS NULL AND {pp} IS NOT NULL)) "
        f"AND (({pp} IS NULL AND {side} IS NULL) OR ({pp} IS NOT NULL AND {side} IN ('front', 'rear')))"
    )


class CableRun(Base):
    """
    Cable Run - represents a physical cable between two ports.
    Each end is a switch port, a server port, or one side (front or rear) of a patch panel port.
    Exactly one of (end_x_switch_port_id, end_x_server_port_id, end_x_patch_port_id) is set per end;
    end_x_patch_side is set exactly when end_x_patch_port_id is. A patch panel port carries at most
    one cable per side (checked in CableRunDAO).
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
    end_a_patch_port_id = Column(
        Integer,
        ForeignKey("patch_panel_ports.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    end_a_patch_side = Column(String(5), nullable=True)
    end_b_patch_port_id = Column(
        Integer,
        ForeignKey("patch_panel_ports.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    end_b_patch_side = Column(String(5), nullable=True)
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
    end_a_patch_port = relationship("PatchPanelPort", foreign_keys=[end_a_patch_port_id])
    end_b_patch_port = relationship("PatchPanelPort", foreign_keys=[end_b_patch_port_id])

    __table_args__ = (
        CheckConstraint(_one_end_sql("a"), name="ck_cable_run_end_a_one"),
        CheckConstraint(_one_end_sql("b"), name="ck_cable_run_end_b_one"),
        UniqueConstraint("end_a_patch_port_id", "end_a_patch_side", name="uq_cable_run_end_a_patch"),
        UniqueConstraint("end_b_patch_port_id", "end_b_patch_side", name="uq_cable_run_end_b_patch"),
    )

    def end(self, which: str):
        """(type, port id, side) for end 'a' or 'b'; side is None unless the end is a patch panel port."""
        switch_id = getattr(self, f"end_{which}_switch_port_id")
        if switch_id is not None:
            return ("switch", switch_id, None)
        server_id = getattr(self, f"end_{which}_server_port_id")
        if server_id is not None:
            return ("server", server_id, None)
        return ("patch", getattr(self, f"end_{which}_patch_port_id"), getattr(self, f"end_{which}_patch_side"))

    def ends(self):
        return (self.end("a"), self.end("b"))

    def get_other_end_ref(self, port_type: str, port_id: int, side: str | None = None):
        """(type, id, side) of the end that is not the given port (and side, for patch ports), or None."""
        a, b = self.ends()
        for this, other in ((a, b), (b, a)):
            if this[0] == port_type and this[1] == port_id and (port_type != "patch" or side is None or this[2] == side):
                return other
        return None

    def get_other_end(self, switch_port_id=None, server_port_id=None, patch_port_id=None, patch_side=None):
        """Return (type, id) for the end that is not the given port. type is 'switch', 'server' or 'patch'."""
        if switch_port_id is not None:
            ref = self.get_other_end_ref("switch", switch_port_id)
        elif server_port_id is not None:
            ref = self.get_other_end_ref("server", server_port_id)
        elif patch_port_id is not None:
            ref = self.get_other_end_ref("patch", patch_port_id, patch_side)
        else:
            ref = None
        return (ref[0], ref[1]) if ref else None

    def __repr__(self):
        def label(ref):
            return f"{ref[0]}:{ref[1]}" + (f"/{ref[2]}" if ref[2] else "")
        a, b = self.ends()
        return f"<CableRun(id={self.id}, {label(a)} <-> {label(b)})>"
