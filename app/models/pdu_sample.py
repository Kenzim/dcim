"""
PDU power history: raw samples written on every successful poll, and coarser rollups.

Four kinds of series exist: the whole PDU, each inlet, each bank (branch) and each outlet of a
PDU that meters outlets. Raw tables are keyed by (series, sampled_at) with no surrogate id, which
keeps the rows and indexes small. Rollup tables hold one row per series and bucket (300 s or 3600 s)
with the average, minimum and maximum of each metric and the energy counter at the start and end
of the bucket. SAMPLE_KINDS describes the tables so the DAO and service handle all four the same way.
"""
from dataclasses import dataclass
from typing import Tuple, Type

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
)

from app.core.database import Base

BUCKET_5M = 300
BUCKET_1H = 3600

# Energy counters can reach tens of thousands of kWh; single precision would lose the deltas.
Counter = Float(53)


def _series_id(table: str):
    return Column(Integer, ForeignKey(f"{table}.id", ondelete="CASCADE"), primary_key=True)


# ---------------------------------------------------------------------------
# Whole PDU
# ---------------------------------------------------------------------------

class PduSample(Base):
    __tablename__ = "pdu_samples"

    pdu_id = _series_id("pdus")
    sampled_at = Column(DateTime(timezone=True), primary_key=True)
    power_w = Column(Float, nullable=True)
    current_a = Column(Float, nullable=True)
    apparent_power_va = Column(Float, nullable=True)
    voltage_v = Column(Float, nullable=True)
    energy_kwh = Column(Counter, nullable=True)

    __table_args__ = (Index("ix_pdu_samples_sampled_at", "sampled_at"),)


class PduRollup(Base):
    __tablename__ = "pdu_rollups"

    pdu_id = _series_id("pdus")
    bucket_seconds = Column(SmallInteger, primary_key=True)
    bucket_start = Column(DateTime(timezone=True), primary_key=True)
    sample_count = Column(Integer, nullable=False)
    power_w_avg = Column(Float, nullable=True)
    power_w_min = Column(Float, nullable=True)
    power_w_max = Column(Float, nullable=True)
    current_a_avg = Column(Float, nullable=True)
    current_a_min = Column(Float, nullable=True)
    current_a_max = Column(Float, nullable=True)
    apparent_power_va_avg = Column(Float, nullable=True)
    apparent_power_va_min = Column(Float, nullable=True)
    apparent_power_va_max = Column(Float, nullable=True)
    voltage_v_avg = Column(Float, nullable=True)
    voltage_v_min = Column(Float, nullable=True)
    voltage_v_max = Column(Float, nullable=True)
    energy_kwh_first = Column(Counter, nullable=True)
    energy_kwh_last = Column(Counter, nullable=True)

    __table_args__ = (Index("ix_pdu_rollups_bucket", "bucket_seconds", "bucket_start"),)


# ---------------------------------------------------------------------------
# Inlets
# ---------------------------------------------------------------------------

class PduInletSample(Base):
    __tablename__ = "pdu_inlet_samples"

    inlet_id = _series_id("pdu_inlets")
    sampled_at = Column(DateTime(timezone=True), primary_key=True)
    power_w = Column(Float, nullable=True)
    current_a = Column(Float, nullable=True)
    apparent_power_va = Column(Float, nullable=True)
    voltage_v = Column(Float, nullable=True)
    energy_kwh = Column(Counter, nullable=True)
    active = Column(Boolean, nullable=True)  # ATS: True while this input carries the load

    __table_args__ = (Index("ix_pdu_inlet_samples_sampled_at", "sampled_at"),)


class PduInletRollup(Base):
    __tablename__ = "pdu_inlet_rollups"

    inlet_id = _series_id("pdu_inlets")
    bucket_seconds = Column(SmallInteger, primary_key=True)
    bucket_start = Column(DateTime(timezone=True), primary_key=True)
    sample_count = Column(Integer, nullable=False)
    power_w_avg = Column(Float, nullable=True)
    power_w_min = Column(Float, nullable=True)
    power_w_max = Column(Float, nullable=True)
    current_a_avg = Column(Float, nullable=True)
    current_a_min = Column(Float, nullable=True)
    current_a_max = Column(Float, nullable=True)
    apparent_power_va_avg = Column(Float, nullable=True)
    apparent_power_va_min = Column(Float, nullable=True)
    apparent_power_va_max = Column(Float, nullable=True)
    voltage_v_avg = Column(Float, nullable=True)
    voltage_v_min = Column(Float, nullable=True)
    voltage_v_max = Column(Float, nullable=True)
    energy_kwh_first = Column(Counter, nullable=True)
    energy_kwh_last = Column(Counter, nullable=True)

    __table_args__ = (Index("ix_pdu_inlet_rollups_bucket", "bucket_seconds", "bucket_start"),)


# ---------------------------------------------------------------------------
# Banks (branches)
# ---------------------------------------------------------------------------

class PduBankSample(Base):
    __tablename__ = "pdu_bank_samples"

    pdu_id = _series_id("pdus")
    bank = Column(String(64), primary_key=True)
    sampled_at = Column(DateTime(timezone=True), primary_key=True)
    current_a = Column(Float, nullable=True)
    power_w = Column(Float, nullable=True)
    rated_current_a = Column(Float, nullable=True)

    __table_args__ = (Index("ix_pdu_bank_samples_sampled_at", "sampled_at"),)


class PduBankRollup(Base):
    __tablename__ = "pdu_bank_rollups"

    pdu_id = _series_id("pdus")
    bank = Column(String(64), primary_key=True)
    bucket_seconds = Column(SmallInteger, primary_key=True)
    bucket_start = Column(DateTime(timezone=True), primary_key=True)
    sample_count = Column(Integer, nullable=False)
    current_a_avg = Column(Float, nullable=True)
    current_a_min = Column(Float, nullable=True)
    current_a_max = Column(Float, nullable=True)
    power_w_avg = Column(Float, nullable=True)
    power_w_min = Column(Float, nullable=True)
    power_w_max = Column(Float, nullable=True)
    rated_current_a = Column(Float, nullable=True)

    __table_args__ = (Index("ix_pdu_bank_rollups_bucket", "bucket_seconds", "bucket_start"),)


# ---------------------------------------------------------------------------
# Outlets (only PDUs with outlet_metering)
# ---------------------------------------------------------------------------

class PduOutletSample(Base):
    __tablename__ = "pdu_outlet_samples"

    outlet_id = _series_id("pdu_outlets")
    sampled_at = Column(DateTime(timezone=True), primary_key=True)
    current_a = Column(Float, nullable=True)
    power_w = Column(Float, nullable=True)

    __table_args__ = (Index("ix_pdu_outlet_samples_sampled_at", "sampled_at"),)


class PduOutletRollup(Base):
    __tablename__ = "pdu_outlet_rollups"

    outlet_id = _series_id("pdu_outlets")
    bucket_seconds = Column(SmallInteger, primary_key=True)
    bucket_start = Column(DateTime(timezone=True), primary_key=True)
    sample_count = Column(Integer, nullable=False)
    current_a_avg = Column(Float, nullable=True)
    current_a_min = Column(Float, nullable=True)
    current_a_max = Column(Float, nullable=True)
    power_w_avg = Column(Float, nullable=True)
    power_w_min = Column(Float, nullable=True)
    power_w_max = Column(Float, nullable=True)

    __table_args__ = (Index("ix_pdu_outlet_rollups_bucket", "bucket_seconds", "bucket_start"),)


@dataclass(frozen=True)
class SampleKind:
    """Describes one series kind: its tables, key columns and what is stored per sample."""

    name: str
    raw: Type
    rollup: Type
    key_cols: Tuple[str, ...]
    metrics: Tuple[str, ...]  # averaged, with min and max kept in rollups
    counter: str | None = None  # monotonic energy counter, kept as first/last per bucket
    latest_cols: Tuple[str, ...] = ()  # copied from the newest sample in a bucket, not averaged


SAMPLE_KINDS = {
    "pdu": SampleKind(
        "pdu", PduSample, PduRollup, ("pdu_id",),
        ("power_w", "current_a", "apparent_power_va", "voltage_v"), counter="energy_kwh",
    ),
    "inlet": SampleKind(
        "inlet", PduInletSample, PduInletRollup, ("inlet_id",),
        ("power_w", "current_a", "apparent_power_va", "voltage_v"), counter="energy_kwh",
    ),
    "bank": SampleKind(
        "bank", PduBankSample, PduBankRollup, ("pdu_id", "bank"),
        ("current_a", "power_w"), latest_cols=("rated_current_a",),
    ),
    "outlet": SampleKind(
        "outlet", PduOutletSample, PduOutletRollup, ("outlet_id",), ("current_a", "power_w"),
    ),
}
