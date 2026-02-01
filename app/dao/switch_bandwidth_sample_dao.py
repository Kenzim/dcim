"""DAO for SwitchBandwidthSample."""
from datetime import datetime
from typing import List, Optional

from sqlalchemy.orm import Session

from app.models.switch_bandwidth_sample import SwitchBandwidthSample


class SwitchBandwidthSampleDAO:
    """Data access for switch bandwidth samples."""

    @staticmethod
    def create(
        db: Session,
        switch_id: int,
        port_identifier: str,
        bytes_in: int,
        bytes_out: int,
        if_index: Optional[int] = None,
        in_errors: int = 0,
        out_errors: int = 0,
        in_discards: int = 0,
        out_discards: int = 0,
        sampled_at: Optional[datetime] = None,
    ) -> SwitchBandwidthSample:
        """Insert a single bandwidth sample."""
        sample = SwitchBandwidthSample(
            switch_id=switch_id,
            port_identifier=port_identifier,
            if_index=if_index,
            bytes_in=bytes_in,
            bytes_out=bytes_out,
            in_errors=in_errors,
            out_errors=out_errors,
            in_discards=in_discards,
            out_discards=out_discards,
            sampled_at=sampled_at,
        )
        db.add(sample)
        return sample

    @staticmethod
    def create_many(db: Session, samples: List[dict]) -> None:
        """Bulk insert bandwidth samples. Each dict must have switch_id, port_identifier, bytes_in, bytes_out, and optional fields."""
        if not samples:
            return
        objs = [
            SwitchBandwidthSample(
                switch_id=s["switch_id"],
                port_identifier=s["port_identifier"],
                if_index=s.get("if_index"),
                bytes_in=s.get("bytes_in", 0),
                bytes_out=s.get("bytes_out", 0),
                in_errors=s.get("in_errors", 0),
                out_errors=s.get("out_errors", 0),
                in_discards=s.get("in_discards", 0),
                out_discards=s.get("out_discards", 0),
                sampled_at=s.get("sampled_at"),
            )
            for s in samples
        ]
        db.bulk_save_objects(objs)
        db.commit()

    @staticmethod
    def get_latest_by_switch_port(
        db: Session,
        switch_id: int,
        port_identifier: str,
    ) -> Optional[SwitchBandwidthSample]:
        """Get the most recent sample for a switch port."""
        return (
            db.query(SwitchBandwidthSample)
            .filter(
                SwitchBandwidthSample.switch_id == switch_id,
                SwitchBandwidthSample.port_identifier == port_identifier,
            )
            .order_by(SwitchBandwidthSample.sampled_at.desc())
            .first()
        )

    @staticmethod
    def get_history(
        db: Session,
        switch_id: int,
        port_identifier: Optional[str] = None,
        since: Optional[datetime] = None,
        limit: int = 1000,
    ) -> List[SwitchBandwidthSample]:
        """Get samples for a switch (optionally a single port) within a time range."""
        q = db.query(SwitchBandwidthSample).filter(SwitchBandwidthSample.switch_id == switch_id)
        if port_identifier is not None:
            q = q.filter(SwitchBandwidthSample.port_identifier == port_identifier)
        if since is not None:
            q = q.filter(SwitchBandwidthSample.sampled_at >= since)
        return q.order_by(SwitchBandwidthSample.sampled_at.asc()).limit(limit).all()

    @staticmethod
    def delete_older_than(db: Session, before: datetime) -> int:
        """Delete samples older than the given time. Returns count deleted."""
        deleted = db.query(SwitchBandwidthSample).filter(SwitchBandwidthSample.sampled_at < before).delete()
        db.commit()
        return deleted
