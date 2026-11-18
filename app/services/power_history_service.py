"""
PDU power history: recording samples, rolling them up, pruning, and building graph data.

Storage (app/models/pdu_sample.py): raw samples are written when a poll succeeds, rolled up into
300 s and 3600 s buckets (average, minimum, maximum) by the poller, and pruned after the windows
in Settings (power_history_*). Reads pick raw rows, 5-minute rollups or hourly rollups from the
requested range and fill the newest part from the finer tables, which have not been rolled up yet.

A "point" is the common shape of a raw row or a rollup row:
    {"t": datetime (UTC), "n": samples in it, "w": seconds it spans (0 for a raw sample),
     "m": {metric: (avg, min, max)}, "e": (counter_first, counter_last) | None, "x": {column: latest value}}
"""
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from app.core.config import settings
from app.dao.pdu_dao import PduDAO, PowerFeedDAO, ServerPowerInputDAO
from app.dao.pdu_sample_dao import PduSampleDAO
from app.models.pdu import Pdu, PduInlet
from app.models.pdu_sample import BUCKET_1H, BUCKET_5M, SAMPLE_KINDS, SampleKind
from app.models.power_feed import PowerFeed
from app.models.server import Server

logger = logging.getLogger(__name__)

# One rollup transaction covers at most this much time, so catching up after downtime stays small.
ROLLUP_CHUNK_SECONDS = 6 * 3600
# A bucket is rolled up only this long after it ended, so in-flight polls have been committed.
ROLLUP_SETTLE_SECONDS = 120
# Points per series that a history response aims for.
TARGET_POINTS = 288
RAW_MAX_HOURS = 24

Point = Dict[str, Any]


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _utc(dt: datetime) -> datetime:
    """Datetimes come back naive from some databases; they are always UTC."""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _floor(dt: datetime, seconds: int) -> datetime:
    epoch = int(_utc(dt).timestamp())
    return datetime.fromtimestamp(epoch - epoch % seconds, tz=timezone.utc)


def _now(now: Optional[datetime]) -> datetime:
    return _utc(now) if now else datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

def _num(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def _present(row: Dict[str, Any], skip: Iterable[str]) -> bool:
    return any(v is not None for k, v in row.items() if k not in skip)


def record_samples(
    db: Session,
    pdu: Pdu,
    sampled_at: datetime,
    metered_outlets: Optional[Iterable[int]] = None,
) -> Dict[str, int]:
    """
    Write one raw sample per series from the PDU's freshly applied readings (pdu.last_readings and
    the inlet and outlet columns). Outlets are stored only when the PDU meters them and the poll
    returned a row for them. Adds rows to the session; the caller commits.
    """
    t = _utc(sampled_at).replace(microsecond=0)
    readings = pdu.last_readings or {}
    totals = readings.get("totals") or {}

    inlet_rows = []
    for inlet in pdu.inlets:
        row = {
            "inlet_id": inlet.id,
            "sampled_at": t,
            "power_w": inlet.power_w,
            "current_a": inlet.current_a,
            "apparent_power_va": inlet.apparent_power_va,
            "voltage_v": inlet.voltage_v,
            "energy_kwh": inlet.energy_kwh,
            "active": inlet.active,
        }
        if _present(row, ("inlet_id", "sampled_at")):
            inlet_rows.append(row)

    voltages = [i.voltage_v for i in pdu.inlets if i.voltage_v is not None]
    energy = _num(totals.get("energy_kwh"))
    if energy is None:
        counters = [i.energy_kwh for i in pdu.inlets if i.energy_kwh is not None]
        energy = round(sum(counters), 3) if counters else None
    pdu_row = {
        "pdu_id": pdu.id,
        "sampled_at": t,
        "power_w": _num(totals.get("power_w")),
        "current_a": _num(totals.get("current_a")),
        "apparent_power_va": _num(totals.get("apparent_power_va")),
        "voltage_v": round(sum(voltages) / len(voltages), 1) if voltages else None,
        "energy_kwh": energy,
    }

    bank_rows: Dict[str, Dict[str, Any]] = {}
    for bank in readings.get("banks") or []:
        name = str(bank.get("name") or "").strip()[:64]
        if not name or name in bank_rows:
            continue
        row = {
            "pdu_id": pdu.id,
            "bank": name,
            "sampled_at": t,
            "current_a": _num(bank.get("current_a")),
            "power_w": _num(bank.get("power_w")),
            "rated_current_a": _num(bank.get("rated_current_a")),
        }
        if _present(row, ("pdu_id", "bank", "sampled_at")):
            bank_rows[name] = row

    outlet_rows = []
    if pdu.outlet_metering and metered_outlets is not None:
        positions = set(metered_outlets)
        for outlet in pdu.outlets:
            if outlet.position not in positions:
                continue
            row = {"outlet_id": outlet.id, "sampled_at": t, "current_a": outlet.current_a, "power_w": outlet.power_w}
            if _present(row, ("outlet_id", "sampled_at")):
                outlet_rows.append(row)

    written = {
        "pdu": [pdu_row] if _present(pdu_row, ("pdu_id", "sampled_at")) else [],
        "inlet": inlet_rows,
        "bank": list(bank_rows.values()),
        "outlet": outlet_rows,
    }
    for kind, rows in written.items():
        PduSampleDAO.insert_raw(db, kind, rows)
    return {kind: len(rows) for kind, rows in written.items()}


# ---------------------------------------------------------------------------
# Points: conversion and merging
# ---------------------------------------------------------------------------

def _series_key(kind: SampleKind, row: Any) -> Tuple:
    return tuple(getattr(row, c) for c in kind.key_cols)


def _raw_point(kind: SampleKind, row: Any) -> Point:
    metrics = {}
    for metric in kind.metrics:
        value = getattr(row, metric)
        if value is not None:
            metrics[metric] = (value, value, value)
    counter = getattr(row, kind.counter) if kind.counter else None
    return {
        "t": _utc(row.sampled_at),
        "n": 1,
        "w": 0,
        "m": metrics,
        "e": (counter, counter) if counter is not None else None,
        "x": {c: getattr(row, c) for c in kind.latest_cols if getattr(row, c) is not None},
    }


def _rollup_point(kind: SampleKind, row: Any) -> Point:
    metrics = {}
    for metric in kind.metrics:
        avg = getattr(row, f"{metric}_avg")
        if avg is not None:
            metrics[metric] = (avg, getattr(row, f"{metric}_min"), getattr(row, f"{metric}_max"))
    first = getattr(row, f"{kind.counter}_first") if kind.counter else None
    last = getattr(row, f"{kind.counter}_last") if kind.counter else None
    return {
        "t": _utc(row.bucket_start),
        "n": row.sample_count,
        "w": row.bucket_seconds,
        "m": metrics,
        "e": (first, last) if first is not None and last is not None else None,
        "x": {c: getattr(row, c) for c in kind.latest_cols if getattr(row, c) is not None},
    }


def merge_points(kind: SampleKind, points: Sequence[Point], t: datetime, width: int = 0) -> Point:
    """Combine time-ordered points into one: weighted average, overall min and max, counter span."""
    metrics = {}
    for metric in kind.metrics:
        parts = [(p["m"][metric], p["n"]) for p in points if metric in p["m"]]
        if not parts:
            continue
        weight = sum(n for _, n in parts) or 1
        metrics[metric] = (
            sum(v[0] * n for v, n in parts) / weight,
            min(v[1] for v, _ in parts),
            max(v[2] for v, _ in parts),
        )
    counted = [p["e"] for p in points if p["e"] is not None]
    latest: Dict[str, Any] = {}
    for p in points:
        latest.update(p["x"])
    return {
        "t": t,
        "n": sum(p["n"] for p in points),
        "w": width,
        "m": metrics,
        "e": (counted[0][0], counted[-1][1]) if counted else None,
        "x": latest,
    }


def _group(
    kind: SampleKind, items: Iterable[Tuple[Tuple, Point]], seconds: int
) -> Dict[Tuple, Dict[datetime, List[Point]]]:
    grouped: Dict[Tuple, Dict[datetime, List[Point]]] = {}
    for key, point in sorted(items, key=lambda item: item[1]["t"]):
        grouped.setdefault(key, {}).setdefault(_floor(point["t"], seconds), []).append(point)
    return grouped


def _rollup_rows(kind: SampleKind, bucket_seconds: int, grouped: Dict[Tuple, Dict[datetime, List[Point]]]) -> List[dict]:
    rows = []
    for key, buckets in grouped.items():
        for start, points in buckets.items():
            merged = merge_points(kind, points, start, bucket_seconds)
            row: Dict[str, Any] = dict(zip(kind.key_cols, key))
            row.update(bucket_seconds=bucket_seconds, bucket_start=start, sample_count=merged["n"])
            for metric in kind.metrics:
                avg, low, high = merged["m"].get(metric, (None, None, None))
                row.update({f"{metric}_avg": avg, f"{metric}_min": low, f"{metric}_max": high})
            if kind.counter:
                first, last = merged["e"] or (None, None)
                row.update({f"{kind.counter}_first": first, f"{kind.counter}_last": last})
            for col in kind.latest_cols:
                row[col] = merged["x"].get(col)
            rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Rollups and pruning (run by the poller)
# ---------------------------------------------------------------------------

def _rollup_window(db: Session, kind: SampleKind, bucket_seconds: int, source: Optional[int], start: datetime, end: datetime) -> int:
    """Rebuild the buckets of [start, end) from raw rows (source None) or from finer rollups."""
    if source is None:
        rows = PduSampleDAO.fetch_raw(db, kind.name, start, end)
        items = [(_series_key(kind, r), _raw_point(kind, r)) for r in rows]
    else:
        rows = PduSampleDAO.fetch_rollups(db, kind.name, source, start, end)
        items = [(_series_key(kind, r), _rollup_point(kind, r)) for r in rows]
    out = _rollup_rows(kind, bucket_seconds, _group(kind, items, bucket_seconds))
    PduSampleDAO.replace_rollups(db, kind.name, bucket_seconds, start, end, out)
    db.commit()
    return len(out)


def rollup(db: Session, now: Optional[datetime] = None) -> Dict[str, int]:
    """
    Roll 5-minute buckets up from raw rows, then hours up from the 5-minute buckets, only once a
    bucket ended at least ROLLUP_SETTLE_SECONDS ago. Each run starts from the newest existing
    bucket (not after it), so a sample committed late, such as one from a manual poll, is counted
    by the next run. Re-running writes the same rows. Returns the rollup rows written per series kind.
    """
    now = _now(now)
    written: Dict[str, int] = {}
    for kind in SAMPLE_KINDS.values():
        total = 0
        for bucket_seconds, source in ((BUCKET_5M, None), (BUCKET_1H, BUCKET_5M)):
            end = _floor(now - timedelta(seconds=ROLLUP_SETTLE_SECONDS), bucket_seconds)
            newest = PduSampleDAO.newest_rollup(db, kind.name, bucket_seconds)
            if newest is not None:
                start = _utc(newest)
            else:
                oldest = (
                    PduSampleDAO.oldest_raw(db, kind.name)
                    if source is None
                    else PduSampleDAO.oldest_rollup(db, kind.name, source)
                )
                if oldest is None:
                    continue
                start = _floor(oldest, bucket_seconds)
            while start < end:
                chunk_end = min(end, start + timedelta(seconds=ROLLUP_CHUNK_SECONDS))
                total += _rollup_window(db, kind, bucket_seconds, source, start, chunk_end)
                start = chunk_end
        written[kind.name] = total
    return written


def _delete_cutoff(cutoff: datetime, rolled_to: Optional[datetime]) -> Optional[datetime]:
    """Never delete rows that the next tier has not absorbed yet, nor those of its newest bucket, which is re-rolled."""
    if rolled_to is None:
        return None
    return min(cutoff, _utc(rolled_to))


def prune(db: Session, now: Optional[datetime] = None) -> Dict[str, Dict[str, int]]:
    """
    Delete raw rows, 5-minute rollups and hourly rollups older than their retention windows. Raw
    rows are kept until they are covered by 5-minute rollups, and 5-minute rollups until covered by
    hourly rollups, so a stalled rollup never loses data. One DELETE per table and tier.
    """
    now = _now(now)
    raw_cut = now - timedelta(days=settings.power_history_raw_retention_days)
    m5_cut = now - timedelta(days=settings.power_history_5m_retention_days)
    h1_cut = now - timedelta(days=settings.power_history_1h_retention_days)
    deleted: Dict[str, Dict[str, int]] = {}
    for kind in SAMPLE_KINDS.values():
        newest_5m = PduSampleDAO.newest_rollup(db, kind.name, BUCKET_5M)
        newest_1h = PduSampleDAO.newest_rollup(db, kind.name, BUCKET_1H)
        raw_to = _delete_cutoff(raw_cut, newest_5m)
        m5_to = _delete_cutoff(m5_cut, newest_1h)
        deleted[kind.name] = {
            "raw": PduSampleDAO.delete_raw_before(db, kind.name, raw_to) if raw_to else 0,
            "5m": PduSampleDAO.delete_rollups_before(db, kind.name, BUCKET_5M, m5_to) if m5_to else 0,
            "1h": PduSampleDAO.delete_rollups_before(db, kind.name, BUCKET_1H, h1_cut),
        }
    db.commit()
    return deleted


def run_maintenance(db: Session, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Roll up, then prune. Called from the poller."""
    rolled = rollup(db, now)
    pruned = prune(db, now)
    return {"rolled_up": rolled, "deleted": pruned}


# ---------------------------------------------------------------------------
# Reading: range selection
# ---------------------------------------------------------------------------

def clamp_hours(hours: Any) -> int:
    longest = max(1, settings.power_history_1h_retention_days) * 24
    try:
        value = int(hours)
    except (TypeError, ValueError):
        value = 24
    return max(1, min(value, longest))


def choose_resolution(hours: int) -> str:
    """"raw" up to 24 hours (while raw rows are kept that long), "5m" up to the 5-minute window, else "1h"."""
    if hours <= RAW_MAX_HOURS and hours <= settings.power_history_raw_retention_days * 24:
        return "raw"
    if hours <= settings.power_history_5m_retention_days * 24:
        return "5m"
    return "1h"


_CHAIN = {"raw": (None,), "5m": (BUCKET_5M, None), "1h": (BUCKET_1H, BUCKET_5M, None)}


def _window(hours: Any, now: Optional[datetime], points: int = TARGET_POINTS) -> Dict[str, Any]:
    hours = clamp_hours(hours)
    until = _now(now)
    resolution = choose_resolution(hours)
    step = {"raw": max(1, settings.power_history_sample_seconds), "5m": BUCKET_5M, "1h": BUCKET_1H}[resolution]
    span = max(step, math.ceil(hours * 3600 / max(1, points) / step) * step)
    return {
        "hours": hours,
        "since": until - timedelta(hours=hours),
        "until": until,
        "resolution": resolution,
        "span": span,
    }


def _meta(win: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "hours": win["hours"],
        "from": win["since"].isoformat(),
        "to": win["until"].isoformat(),
        "resolution": win["resolution"],
        "bucket_seconds": win["span"],
    }


def read_points(db: Session, kind_name: str, win: Dict[str, Any], filters: Dict[str, Any]) -> Dict[Tuple, List[Point]]:
    """Points per series over the window, coarsest table first and the newest part from finer tables."""
    kind = SAMPLE_KINDS[kind_name]
    out: Dict[Tuple, List[Point]] = {}
    cursor = win["since"]
    for bucket in _CHAIN[win["resolution"]]:
        if bucket is None:
            rows = PduSampleDAO.fetch_raw(db, kind_name, cursor, win["until"], filters)
            conv = _raw_point
        else:
            rows = PduSampleDAO.fetch_rollups(db, kind_name, bucket, cursor, win["until"], filters)
            conv = _rollup_point
        if not rows:
            continue
        for row in rows:
            out.setdefault(_series_key(kind, row), []).append(conv(kind, row))
        newest = max(p["t"] for pts in out.values() for p in pts if p["w"] == (bucket or 0))
        cursor = newest + timedelta(seconds=bucket or 1)
    for pts in out.values():
        pts.sort(key=lambda p: p["t"])
    return out


# ---------------------------------------------------------------------------
# Reading: bucketing, summing, energy
# ---------------------------------------------------------------------------

def bucketise(kind: SampleKind, points: Sequence[Point], span: int) -> List[Point]:
    grouped: Dict[datetime, List[Point]] = {}
    for p in points:
        grouped.setdefault(_floor(p["t"], span), []).append(p)
    return [merge_points(kind, grouped[t], t, span) for t in sorted(grouped)]


def sum_points(
    kind: SampleKind, series: Sequence[Sequence[Point]], metrics: Sequence[str], expected: Optional[int] = None
) -> List[Point]:
    """
    Add already bucketed series together bucket by bucket (min and max add up to bounds). "c" is the
    number of series that had a point in the bucket; with `expected` (how many series the scope has)
    a bucket where fewer contributed is marked "partial", because its total is a lower bound.
    """
    sums: Dict[datetime, Dict[str, List[float]]] = {}
    counts: Dict[datetime, int] = {}
    for points in series:
        for p in points:
            acc = sums.setdefault(p["t"], {})
            counts[p["t"]] = counts.get(p["t"], 0) + 1
            for metric in metrics:
                if metric in p["m"]:
                    cur = acc.setdefault(metric, [0.0, 0.0, 0.0])
                    for i in range(3):
                        cur[i] += p["m"][metric][i]
    return [
        {
            "t": t, "n": 0, "w": 0, "m": {m: tuple(v) for m, v in acc.items()}, "e": None, "x": {},
            "c": counts[t], "partial": expected is not None and counts[t] < expected,
        }
        for t, acc in sorted(sums.items())
    ]


def _samples(points: Sequence[Point], metric: str, digits: int = 2) -> List[Dict[str, float]]:
    out = []
    for p in points:
        if metric in p["m"]:
            avg, low, high = p["m"][metric]
            sample = {
                "t": int(p["t"].timestamp() * 1000),
                "value": round(avg, digits),
                "min": round(low, digits),
                "max": round(high, digits),
            }
            if p.get("partial"):
                sample["partial"] = True
            out.append(sample)
    return out


def _metrics(points: Sequence[Point], metrics: Sequence[str]) -> Dict[str, List[Dict[str, float]]]:
    return {m: _samples(points, m, 3 if m == "current_a" else 1) for m in metrics}


def energy_kwh(points: Sequence[Point]) -> Dict[str, Any]:
    """
    Energy used over the points. From the PDU's energy counter when at least two points carry one
    (a counter that goes backwards is treated as reset), else by integrating average power over the
    sampled time (each sample stands for power_history_sample_seconds).
    """
    counted = [p["e"] for p in points if p["e"] is not None]
    if len(counted) >= 2 or (len(counted) == 1 and counted[0][1] != counted[0][0]):
        total = 0.0
        previous: Optional[float] = None
        for first, last in counted:
            base = first if previous is None else previous
            delta = last - base
            total += delta if delta >= 0 else last
            previous = last
        return {"kwh": round(total, 3), "method": "counter"}
    watt_seconds = sum(
        p["m"]["power_w"][0] * p["n"] * settings.power_history_sample_seconds for p in points if "power_w" in p["m"]
    )
    if not any("power_w" in p["m"] for p in points):
        return {"kwh": None, "method": None}
    return {"kwh": round(watt_seconds / 3_600_000, 3), "method": "integrated"}


def _combine_energy(parts: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    known = [p for p in parts if p["kwh"] is not None]
    if not known:
        return {"kwh": None, "method": None}
    methods = {p["method"] for p in known}
    return {"kwh": round(sum(p["kwh"] for p in known), 3), "method": methods.pop() if len(methods) == 1 else "mixed"}


# ---------------------------------------------------------------------------
# Reading: history per scope
# ---------------------------------------------------------------------------

SUM_METRICS = ("power_w", "current_a")


def _summed(
    db: Session,
    kind_name: str,
    win: Dict[str, Any],
    filters: Dict[str, Any],
    expected: Optional[int] = None,
) -> Tuple[List[Point], Dict[str, Any], Dict[Tuple, List[Point]]]:
    """
    Bucket each series, sum them, and add their energies. `expected` is the number of series in the
    scope (buckets with fewer are marked partial). Returns (summed points, energy, bucketed series).
    """
    kind = SAMPLE_KINDS[kind_name]
    has_series = all(v != [] for v in filters.values())
    raw = read_points(db, kind_name, win, filters) if has_series else {}
    bucketed = {key: bucketise(kind, pts, win["span"]) for key, pts in raw.items()}
    total = sum_points(kind, list(bucketed.values()), SUM_METRICS, expected)
    energy = _combine_energy([energy_kwh(pts) for pts in raw.values()])
    return total, energy, bucketed


def _rack_pdus(db: Session, rack_id: int) -> List[Pdu]:
    pdus = {p.id: p for p in PduDAO.get_all(db, rack_id=rack_id)}
    for p in PduDAO.get_by_feed_rack(db, rack_id):
        pdus.setdefault(p.id, p)
    return sorted(pdus.values(), key=lambda p: p.name)


def _live_power(pdus: Sequence[Pdu]) -> Optional[float]:
    from app.services.power_service import pdu_power_w

    values = [pdu_power_w(p) for p in pdus if p.enabled]
    present = [v for v in values if v is not None]
    return round(sum(present), 1) if present else None


def _aggregate(
    db: Session, scope: str, scope_id: Optional[int], name: Optional[str], pdus: Sequence[Pdu],
    hours: Any, now: Optional[datetime], points: int = TARGET_POINTS,
) -> Dict[str, Any]:
    win = _window(hours, now, points)
    total, energy, bucketed = _summed(
        db, "pdu", win, {"pdu_id": [p.id for p in pdus]}, sum(1 for p in pdus if p.enabled)
    )
    return {
        "scope": scope,
        "id": scope_id,
        "name": name,
        **_meta(win),
        "pdu_count": len(pdus),
        "pdus_with_data": len(bucketed),
        "current_power_w": _live_power(pdus),
        "metrics": _metrics(total, SUM_METRICS),
        "energy": energy,
        "partial": any(p["partial"] for p in total),
    }


def pdu_history(db: Session, pdu: Pdu, hours: Any = 24, now: Optional[datetime] = None, points: int = TARGET_POINTS) -> Dict[str, Any]:
    """The PDU's own series, each inlet, and each bank with its rated current."""
    win = _window(hours, now, points)
    pdu_kind, inlet_kind, bank_kind = SAMPLE_KINDS["pdu"], SAMPLE_KINDS["inlet"], SAMPLE_KINDS["bank"]
    own = read_points(db, "pdu", win, {"pdu_id": pdu.id}).get((pdu.id,), [])
    inlet_points = read_points(db, "inlet", win, {"inlet_id": [i.id for i in pdu.inlets]}) if pdu.inlets else {}
    bank_points = read_points(db, "bank", win, {"pdu_id": pdu.id})

    inlets = []
    for inlet in pdu.inlets:
        pts = inlet_points.get((inlet.id,), [])
        inlets.append({
            "id": inlet.id,
            "position": inlet.position,
            "name": inlet.name,
            "power_feed_id": inlet.power_feed_id,
            "active": inlet.active,
            "metrics": _metrics(bucketise(inlet_kind, pts, win["span"]), inlet_kind.metrics),
            "energy": energy_kwh(pts),
        })

    live_rated = {str(b.get("name")): _num(b.get("rated_current_a")) for b in (pdu.last_readings or {}).get("banks") or []}
    banks = []
    for (_, bank_name), pts in sorted(bank_points.items(), key=lambda item: item[0][1]):
        bucketed = bucketise(bank_kind, pts, win["span"])
        rated = next((p["x"]["rated_current_a"] for p in reversed(bucketed) if "rated_current_a" in p["x"]), None)
        banks.append({
            "name": bank_name,
            "rated_current_a": rated if rated is not None else live_rated.get(bank_name),
            "metrics": _metrics(bucketed, bank_kind.metrics),
        })

    return {
        "scope": "pdu",
        "id": pdu.id,
        "name": pdu.name,
        **_meta(win),
        "metrics": _metrics(bucketise(pdu_kind, own, win["span"]), pdu_kind.metrics),
        "energy": energy_kwh(own),
        "inlets": inlets,
        "banks": banks,
    }


def inlet_history(db: Session, inlet: PduInlet, hours: Any = 24, now: Optional[datetime] = None, points: int = TARGET_POINTS) -> Dict[str, Any]:
    win = _window(hours, now, points)
    kind = SAMPLE_KINDS["inlet"]
    pts = read_points(db, "inlet", win, {"inlet_id": inlet.id}).get((inlet.id,), [])
    return {
        "scope": "inlet",
        "id": inlet.id,
        "name": inlet.name,
        "pdu_id": inlet.pdu_id,
        "power_feed_id": inlet.power_feed_id,
        "active": inlet.active,
        **_meta(win),
        "metrics": _metrics(bucketise(kind, pts, win["span"]), kind.metrics),
        "energy": energy_kwh(pts),
    }


def feed_history(db: Session, feed: PowerFeed, hours: Any = 24, now: Optional[datetime] = None, points: int = TARGET_POINTS) -> Dict[str, Any]:
    """Sum of the feed's inlets, each inlet's power, and the feed's limits for drawing lines."""
    from app.services.power_service import feed_capacity

    win = _window(hours, now, points)
    inlets = [i for i in feed.pdu_inlets if i.pdu is not None]
    total, energy, bucketed = _summed(
        db, "inlet", win, {"inlet_id": [i.id for i in inlets]}, sum(1 for i in inlets if i.pdu.enabled)
    )
    live = [i.power_w for i in inlets if i.pdu.enabled and i.power_w is not None]
    return {
        "scope": "feed",
        "id": feed.id,
        "name": feed.name,
        **_meta(win),
        "limits": {"amperage": feed.amperage, **feed_capacity(feed)},
        "current_power_w": round(sum(live), 1) if live else None,
        "metrics": _metrics(total, SUM_METRICS),
        "energy": energy,
        "partial": any(p["partial"] for p in total),
        "inlets": [
            {
                "id": i.id,
                "name": i.name,
                "pdu_id": i.pdu_id,
                "pdu_name": i.pdu.name,
                "active": i.active,
                "metrics": _metrics(bucketed.get((i.id,), []), SUM_METRICS),
            }
            for i in inlets
        ],
    }


def rack_history(db: Session, rack_id: int, name: Optional[str], hours: Any = 24, now: Optional[datetime] = None, points: int = TARGET_POINTS) -> Dict[str, Any]:
    """Sum of the rack's PDUs (those in it and those drawing from its feeds), with the rack's feed capacity."""
    from app.services.power_service import feed_capacity

    pdus = _rack_pdus(db, rack_id)
    result = _aggregate(db, "rack", rack_id, name, pdus, hours, now, points)
    caps = [feed_capacity(f) for f in PowerFeedDAO.get_by_rack(db, rack_id)]
    result["limits"] = {
        "capacity_w": round(sum(c["capacity_w"] for c in caps), 1) if caps else None,
        "usable_w": round(sum(c["usable_w"] for c in caps), 1) if caps else None,
    }
    return result


def location_history(db: Session, location_id: Optional[int], name: Optional[str], hours: Any = 24, now: Optional[datetime] = None, points: int = TARGET_POINTS) -> Dict[str, Any]:
    """Sum of every PDU in a location, or of all PDUs when location_id is None."""
    pdus = PduDAO.get_all(db, location_id=location_id)
    scope = "location" if location_id is not None else "all"
    return _aggregate(db, scope, location_id, name, pdus, hours, now, points)


def server_history(db: Session, server: Server, hours: Any = 24, now: Optional[datetime] = None, points: int = TARGET_POINTS) -> Dict[str, Any]:
    """
    Power of a server from the metered outlets its power inputs are linked to. `available` is False
    when none of them can be known (no input, no linked outlet, or the PDU does not meter outlets);
    `complete` is False when some inputs are known and others are not, so the total is a lower bound.
    """
    win = _window(hours, now, points)
    kind = SAMPLE_KINDS["outlet"]
    inputs = []
    outlet_ids: List[int] = []
    for spi in ServerPowerInputDAO.get_by_server(db, server.id):
        outlet, pdu = spi.pdu_outlet, (spi.pdu_outlet.pdu if spi.pdu_outlet else None)
        if outlet is None or pdu is None:
            status, reason = "unlinked", "No PDU outlet is linked to this power input"
        elif not pdu.outlet_metering:
            status, reason = "unmetered", f"PDU {pdu.name} does not meter its outlets"
        else:
            status, reason = "metered", None
            outlet_ids.append(outlet.id)
        inputs.append({
            "id": spi.id,
            "name": spi.name,
            "pdu_name": pdu.name if pdu else None,
            "outlet_name": outlet.name if outlet else None,
            "status": status,
            "reason": reason,
        })

    metered = len(outlet_ids)
    available = metered > 0
    complete = bool(inputs) and metered == len(inputs)
    if not inputs:
        reason = "This server has no power inputs"
    elif not available:
        reason = "No power input is linked to a PDU outlet that reports power"
    elif not complete:
        reason = "Some power inputs are not metered, so this is only part of the server's draw"
    else:
        reason = None

    total: List[Point] = []
    energy: Dict[str, Any] = {"kwh": None, "method": None}
    if available:
        total, energy, _ = _summed(db, "outlet", win, {"outlet_id": outlet_ids}, len(outlet_ids))
    return {
        "scope": "server",
        "id": server.id,
        "name": server.name,
        **_meta(win),
        "available": available,
        "complete": complete,
        "reason": reason,
        "inputs": inputs,
        "metrics": _metrics(total, kind.metrics),
        "energy": energy,
        "partial": any(p["partial"] for p in total),
    }
