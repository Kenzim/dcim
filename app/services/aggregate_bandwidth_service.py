"""Aggregate in/out bandwidth rates across monitored (tracked) server ports."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from sqlalchemy.orm import Session

from app.dao.cable_run_dao import CableRunDAO
from app.dao.network_port_dao import NetworkPortDAO
from app.dao.switch_bandwidth_sample_dao import SwitchBandwidthSampleDAO
from app.dao.switch_port_dao import SwitchPortDAO


def bucket_span_ms(hours: int, target_points: int = 96) -> int:
    """Choose a bucket width that yields ~target_points over the window (min 60s)."""
    total_ms = max(1, hours) * 3600 * 1000
    return max(60_000, round(total_ms / target_points))


def _counter_delta(current: int, previous: int) -> int:
    """Bytes transferred between samples; treat counter wrap as a fresh absolute value."""
    delta = current - previous
    return current if delta < 0 else delta


def _rate_point(prev: Any, current: Any) -> Dict[str, float] | None:
    """Build one in/out Mbps point from two consecutive counter samples."""
    if not prev or not current or not current.sampled_at or not prev.sampled_at:
        return None
    try:
        dt = (current.sampled_at - prev.sampled_at).total_seconds()
    except (TypeError, ValueError, OverflowError, AttributeError):
        return None
    if dt <= 0:
        return None
    return {
        "t": current.sampled_at.timestamp() * 1000,
        "rate_in_mbps": _counter_delta(current.bytes_in, prev.bytes_in) * 8 / 1_000_000 / dt,
        "rate_out_mbps": _counter_delta(current.bytes_out, prev.bytes_out) * 8 / 1_000_000 / dt,
    }


def rates_from_samples(samples: Sequence[Any]) -> List[Dict[str, float]]:
    """Convert cumulative counter samples into per-interval in/out Mbps points."""
    points: List[Dict[str, float]] = []
    prev = None
    for sample in samples:
        point = _rate_point(prev, sample)
        if point is not None:
            points.append(point)
        prev = sample
    return points


def aggregate_in_out(
    port_rate_series: Iterable[Sequence[Dict[str, float]]],
    span_ms: int,
) -> Tuple[List[Dict[str, float]], List[Dict[str, float]]]:
    """Sum in and out Mbps across ports into aligned time buckets."""
    in_buckets: Dict[int, float] = {}
    out_buckets: Dict[int, float] = {}
    for series in port_rate_series:
        for point in series:
            t = point.get("t")
            if t is None:
                continue
            bucket_t = int(t // span_ms) * span_ms
            in_buckets[bucket_t] = in_buckets.get(bucket_t, 0.0) + float(point.get("rate_in_mbps") or 0)
            out_buckets[bucket_t] = out_buckets.get(bucket_t, 0.0) + float(point.get("rate_out_mbps") or 0)

    def to_samples(buckets: Dict[int, float]) -> List[Dict[str, float]]:
        return [{"t": t, "value": round(v, 4)} for t, v in sorted(buckets.items())]

    return to_samples(in_buckets), to_samples(out_buckets)


def resolve_monitored_switch_ports(db: Session) -> List[Tuple[int, str]]:
    """
    Return unique (switch_id, port_identifier) for server ports with
    monitor_bandwidth enabled that are cabled to a switch port.
    """
    seen = set()
    resolved: List[Tuple[int, str]] = []
    for port in NetworkPortDAO.get_monitored(db):
        cable = CableRunDAO.get_by_server_port(db, port.id)
        if not cable:
            continue
        other = cable.get_other_end(server_port_id=port.id)
        if not other or other[0] != "switch":
            continue
        switch_port = SwitchPortDAO.get_by_id(db, other[1])
        if not switch_port or not switch_port.switch_id or not switch_port.name:
            continue
        key = (switch_port.switch_id, switch_port.name)
        if key in seen:
            continue
        seen.add(key)
        resolved.append(key)
    return resolved


def get_aggregate_monitored_bandwidth(db: Session, hours: int = 24) -> Dict[str, Any]:
    """Build aggregate Traffic in / Traffic out series for monitored server ports."""
    hours = max(1, min(int(hours or 24), 168))
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    span_ms = bucket_span_ms(hours)
    targets = resolve_monitored_switch_ports(db)

    port_series: List[List[Dict[str, float]]] = []
    for switch_id, port_identifier in targets:
        raw = SwitchBandwidthSampleDAO.get_history(
            db,
            switch_id=switch_id,
            port_identifier=port_identifier,
            since=since,
            limit=5000,
        )
        rates = rates_from_samples(raw)
        if rates:
            port_series.append(rates)

    in_samples, out_samples = aggregate_in_out(port_series, span_ms)
    return {
        "hours": hours,
        "tracked_ports": len(targets),
        "series": [
            {"id": "in", "label": "Traffic in", "samples": in_samples},
            {"id": "out", "label": "Traffic out", "samples": out_samples},
        ],
    }
