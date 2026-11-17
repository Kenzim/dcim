"""Server power input (PSU) model, optionally plugged into a PDU outlet."""
from sqlalchemy import Column, Integer, String, Text, ForeignKey
from sqlalchemy.orm import relationship, backref
from app.core.database import Base


class ServerPowerInput(Base):
    """A power supply input on a server. Each PDU outlet feeds at most one input."""
    __tablename__ = "server_power_inputs"

    id = Column(Integer, primary_key=True, index=True)
    server_id = Column(Integer, ForeignKey("servers.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(64), nullable=False)
    pdu_outlet_id = Column(
        Integer,
        ForeignKey("pdu_outlets.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
        index=True,
    )
    description = Column(Text, nullable=True)

    server = relationship(
        "Server",
        backref=backref(
            "power_inputs",
            cascade="all, delete-orphan",
            passive_deletes=True,
            order_by="ServerPowerInput.id",
        ),
    )
    pdu_outlet = relationship("PduOutlet", backref="server_power_inputs")

    def __repr__(self):
        return f"<ServerPowerInput(id={self.id}, server_id={self.server_id}, name='{self.name}', outlet={self.pdu_outlet_id})>"
