"""PDU power history: sample writing, rollups, pruning, range selection, energy and the per-scope views."""
from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import settings
from app.dao.pdu_sample_dao import PduSampleDAO
from app.models.location import Location
from app.models.pdu import Pdu, PduInlet, PduOutlet
from app.models.pdu_sample import (
    BUCKET_1H,
    BUCKET_5M,
    PduBankSample,
    PduInletSample,
    PduOutletSample,
    PduRollup,
    PduSample,
)
from app.models.power_feed import PowerFeed
from app.models.rack import Rack
from app.services import power_history_service as history
from app.services import power_service

NOW = datetime(2026, 11, 18, 9, 7, 30, tzinfo=timezone.utc)
START = datetime(2026, 11, 18, 6, 0, tzinfo=timezone.utc)


@pytest.fixture
def site(db_session):
    loc = Location(name="DC1", description="")
    db_session.add(loc)
    db_session.flush()
    rack = Rack(location_id=loc.id, name="R1", units=42)
    db_session.add(rack)
    db_session.commit()
    return loc, rack


def make_pdu(db, site, name="pdu-a", metered=True, inlets=1, outlets=2):
    loc, rack = site
    pdu = Pdu(
        name=name, location_id=loc.id, rack_id=rack.id, plugin_name="x", plugin_config={},
        inlet_count=inlets, outlet_count=outlets, outlet_metering=metered,
    )
    db.add(pdu)
    db.flush()
    for i in range(1, inlets + 1):
        db.add(PduInlet(pdu_id=pdu.id, position=i, name=f"Input {i}"))
    for o in range(1, outlets + 1):
        db.add(PduOutlet(pdu_id=pdu.id, position=o, name=f"Outlet {o}", bank="A"))
    db.commit()
    db.refresh(pdu)
    return pdu


def readings(power=1000.0, energy=None, outlets=True):
    data = {
        "inlets": [{"position": 1, "voltage_v": 208.0, "current_a": power / 208.0, "power_w": power, "energy_kwh": energy}],
        "banks": [{"name": "A", "current_a": 4.0, "rated_current_a": 16, "power_w": 800.0}, {"name": "B", "current_a": 1.5}],
        "totals": {"power_w": power, "current_a": power / 208.0, "energy_kwh": energy},
    }
    if outlets:
        data["outlets"] = [
            {"position": 1, "current_a": 1.0, "power_w": power / 4, "state": "on"},
            {"position": 2, "current_a": 2.0, "power_w": power / 2, "state": "on"},
        ]
    return data


def put_raw(db, pdu_id, start, minutes, power=lambda m: 100.0 + m, energy=None):
    rows = [
        {
            "pdu_id": pdu_id,
            "sampled_at": start + timedelta(minutes=m),
            "power_w": power(m),
            "current_a": power(m) / 200.0,
            "energy_kwh": energy(m) if energy else None,
        }
        for m in range(minutes)
    ]
    PduSampleDAO.insert_raw(db, "pdu", rows)
    db.commit()


def rollup_rows(db, bucket):
    return (
        db.query(PduRollup).filter(PduRollup.bucket_seconds == bucket).order_by(PduRollup.bucket_start).all()
    )


def snapshot(db, bucket):
    return [
        (r.pdu_id, r.bucket_start, r.sample_count, r.power_w_avg, r.power_w_min, r.power_w_max, r.energy_kwh_first, r.energy_kwh_last)
        for r in rollup_rows(db, bucket)
    ]


# ---------------------------------------------------------------------------
# Writing samples
# ---------------------------------------------------------------------------

def test_apply_readings_writes_one_sample_per_series(db_session, site):
    pdu = make_pdu(db_session, site)
    power_service.apply_readings(db_session, pdu, readings(energy=1234.5), now=NOW)

    row = db_session.query(PduSample).one()
    assert (row.power_w, row.voltage_v, row.energy_kwh) == (1000.0, 208.0, 1234.5)
    assert row.sampled_at.replace(tzinfo=timezone.utc) == NOW.replace(microsecond=0)
    inlet = db_session.query(PduInletSample).one()
    assert inlet.power_w == 1000.0 and inlet.energy_kwh == 1234.5
    banks = {b.bank: b for b in db_session.query(PduBankSample).all()}
    assert set(banks) == {"A", "B"}
    assert banks["A"].rated_current_a == 16.0 and banks["A"].current_a == 4.0
    outlets = db_session.query(PduOutletSample).order_by(PduOutletSample.outlet_id).all()
    assert [o.power_w for o in outlets] == [250.0, 500.0]


def test_outlets_only_recorded_when_pdu_meters_them(db_session, site):
    pdu = make_pdu(db_session, site, metered=False)
    power_service.apply_readings(db_session, pdu, readings(), now=NOW)
    assert db_session.query(PduSample).count() == 1
    assert db_session.query(PduOutletSample).count() == 0


def test_outlets_without_a_reading_row_are_skipped(db_session, site):
    pdu = make_pdu(db_session, site)
    data = readings()
    data["outlets"] = data["outlets"][:1]
    power_service.apply_readings(db_session, pdu, data, now=NOW)
    assert db_session.query(PduOutletSample).count() == 1


def test_two_polls_in_the_same_second_write_one_sample(db_session, site):
    pdu = make_pdu(db_session, site)
    power_service.apply_readings(db_session, pdu, readings(), now=NOW)
    power_service.apply_readings(db_session, pdu, readings(1100.0), now=NOW + timedelta(milliseconds=300))
    assert db_session.query(PduSample).count() == 1
    assert pdu.last_readings["totals"]["power_w"] == 1100.0  # latest values still updated
    power_service.apply_readings(db_session, pdu, readings(1200.0), now=NOW + timedelta(seconds=60))
    assert db_session.query(PduSample).count() == 2


def test_history_failure_does_not_block_latest_values(db_session, site, monkeypatch):
    from sqlalchemy.exc import OperationalError

    def boom(*args, **kwargs):
        raise OperationalError("insert", {}, Exception("disk full"))

    pdu = make_pdu(db_session, site)
    monkeypatch.setattr(power_service, "record_samples", boom)
    power_service.apply_readings(db_session, pdu, readings(), now=NOW)
    assert pdu.last_readings["totals"]["power_w"] == 1000.0
    assert pdu.last_polled_at is not None
    assert db_session.query(PduSample).count() == 0


def test_ats_inactive_inlet_records_zero(db_session, site):
    pdu = make_pdu(db_session, site, name="ats", inlets=2, outlets=0, metered=False)
    power_service.apply_readings(
        db_session,
        pdu,
        {
            "inlets": [{"position": 1, "voltage_v": 207.0, "active": False}, {"position": 2, "voltage_v": 208.0, "active": True}],
            "totals": {"current_a": 8.0},
        },
        now=NOW,
    )
    by_position = {i.position: i for i in pdu.inlets}
    rows = {r.inlet_id: r for r in db_session.query(PduInletSample).all()}
    assert rows[by_position[1].id].power_w == 0.0 and rows[by_position[1].id].active is False
    assert rows[by_position[2].id].power_w == pytest.approx(1664.0) and rows[by_position[2].id].active is True
    assert db_session.query(PduSample).one().current_a == 8.0


# ---------------------------------------------------------------------------
# Rollups
# ---------------------------------------------------------------------------

def test_rollup_builds_5_minute_and_hourly_buckets(db_session, site):
    pdu = make_pdu(db_session, site)
    put_raw(db_session, pdu.id, START, 190)  # 06:00 to 09:09

    written = history.rollup(db_session, NOW)

    five = rollup_rows(db_session, BUCKET_5M)
    assert len(five) == 37  # 06:00 .. 09:00, the 09:05 bucket is not complete at 09:07
    first = five[0]
    assert (first.sample_count, first.power_w_min, first.power_w_max) == (5, 100.0, 104.0)
    assert first.power_w_avg == pytest.approx(102.0)
    hourly = rollup_rows(db_session, BUCKET_1H)
    assert len(hourly) == 3  # 06, 07, 08
    assert (hourly[0].sample_count, hourly[0].power_w_min, hourly[0].power_w_max) == (60, 100.0, 159.0)
    assert hourly[0].power_w_avg == pytest.approx(129.5)
    assert written["pdu"] == 40


def test_rollup_rerun_is_a_no_op_and_windows_are_idempotent(db_session, site):
    pdu = make_pdu(db_session, site)
    put_raw(db_session, pdu.id, START, 190)
    history.rollup(db_session, NOW)
    before_5m, before_1h = snapshot(db_session, BUCKET_5M), snapshot(db_session, BUCKET_1H)

    assert history.rollup(db_session, NOW)["pdu"] == 2  # only the newest 5-minute and hourly buckets are re-rolled
    assert snapshot(db_session, BUCKET_5M) == before_5m
    assert snapshot(db_session, BUCKET_1H) == before_1h

    kind = history.SAMPLE_KINDS["pdu"]
    end = START + timedelta(hours=3)
    history._rollup_window(db_session, kind, BUCKET_5M, None, START, end)
    history._rollup_window(db_session, kind, BUCKET_1H, BUCKET_5M, START, end)
    assert snapshot(db_session, BUCKET_5M) == before_5m
    assert snapshot(db_session, BUCKET_1H) == before_1h


def test_rollup_continues_from_the_newest_bucket(db_session, site):
    pdu = make_pdu(db_session, site)
    put_raw(db_session, pdu.id, START, 190)
    history.rollup(db_session, NOW)
    put_raw(db_session, pdu.id, START + timedelta(minutes=190), 60)  # up to 10:09

    history.rollup(db_session, NOW + timedelta(hours=1))

    assert len(rollup_rows(db_session, BUCKET_5M)) == 49  # 06:00 .. 10:00
    assert len(rollup_rows(db_session, BUCKET_1H)) == 4  # 06, 07, 08, 09


def test_rollup_covers_a_backlog_longer_than_one_chunk(db_session, site):
    pdu = make_pdu(db_session, site)
    put_raw(db_session, pdu.id, START - timedelta(hours=20), 22 * 60)
    history.rollup(db_session, NOW)
    first = rollup_rows(db_session, BUCKET_5M)[0]
    assert first.bucket_start.replace(tzinfo=timezone.utc) == START - timedelta(hours=20)
    assert len(rollup_rows(db_session, BUCKET_5M)) == 22 * 12  # 22 hours of data, rolled up in four 6-hour chunks


def test_hourly_rollup_keeps_counter_and_weights_by_samples(db_session, site):
    pdu = make_pdu(db_session, site)
    put_raw(db_session, pdu.id, START, 190, power=lambda m: 100.0, energy=lambda m: 50.0 + m * 0.01)
    history.rollup(db_session, NOW)
    hour = rollup_rows(db_session, BUCKET_1H)[0]
    assert hour.energy_kwh_first == pytest.approx(50.0) and hour.energy_kwh_last == pytest.approx(50.59)


def test_late_sample_in_an_already_rolled_bucket_is_counted_by_the_next_run(db_session, site):
    pdu = make_pdu(db_session, site)
    put_raw(db_session, pdu.id, START, 190, power=lambda m: 100.0)
    history.rollup(db_session, NOW)
    newest = rollup_rows(db_session, BUCKET_5M)[-1]
    assert newest.bucket_start.replace(tzinfo=timezone.utc) == datetime(2026, 11, 18, 9, 0, tzinfo=timezone.utc)
    assert newest.sample_count == 5

    # a manual poll commits a sample into the 09:00 bucket after that bucket was rolled up
    PduSampleDAO.insert_raw(db_session, "pdu", [{"pdu_id": pdu.id, "sampled_at": datetime(2026, 11, 18, 9, 4, 30, tzinfo=timezone.utc), "power_w": 600.0}])
    db_session.commit()
    history.rollup(db_session, NOW + timedelta(minutes=1))

    newest = rollup_rows(db_session, BUCKET_5M)[-1]
    assert newest.sample_count == 6 and newest.power_w_max == 600.0
    hour = rollup_rows(db_session, BUCKET_1H)[-1]  # the 08:00 hour was rolled before and does not include 09:00 yet
    assert hour.bucket_start.replace(tzinfo=timezone.utc) == datetime(2026, 11, 18, 8, 0, tzinfo=timezone.utc)

    # the hour containing the late sample, once complete, includes it
    history.rollup(db_session, datetime(2026, 11, 18, 10, 5, tzinfo=timezone.utc))
    nine = rollup_rows(db_session, BUCKET_1H)[-1]
    assert nine.bucket_start.replace(tzinfo=timezone.utc) == datetime(2026, 11, 18, 9, 0, tzinfo=timezone.utc)
    assert nine.power_w_max == 600.0


def test_buckets_are_rolled_only_after_a_two_minute_settle_time(db_session, site):
    pdu = make_pdu(db_session, site)
    put_raw(db_session, pdu.id, START, 190)
    history.rollup(db_session, datetime(2026, 11, 18, 9, 5, 59, tzinfo=timezone.utc))  # 09:00 bucket ended 59 s ago
    assert rollup_rows(db_session, BUCKET_5M)[-1].bucket_start.replace(tzinfo=timezone.utc) == datetime(2026, 11, 18, 8, 55, tzinfo=timezone.utc)
    history.rollup(db_session, datetime(2026, 11, 18, 9, 7, 0, tzinfo=timezone.utc))
    assert rollup_rows(db_session, BUCKET_5M)[-1].bucket_start.replace(tzinfo=timezone.utc) == datetime(2026, 11, 18, 9, 0, tzinfo=timezone.utc)


def test_prune_never_removes_the_source_rows_of_the_newest_bucket(db_session, site, monkeypatch):
    monkeypatch.setattr(settings, "power_history_raw_retention_days", 1)
    pdu = make_pdu(db_session, site)
    old = START - timedelta(days=5)
    put_raw(db_session, pdu.id, old, 60)
    history.rollup(db_session, old + timedelta(hours=1))  # poller then stops for days
    history.prune(db_session, NOW)
    history.rollup(db_session, NOW)
    first = rollup_rows(db_session, BUCKET_5M)[0]
    assert first.sample_count == 5  # the old buckets were not emptied by a later re-roll


# ---------------------------------------------------------------------------
# Pruning
# ---------------------------------------------------------------------------

def count(db, bucket=None):
    if bucket is None:
        return db.query(PduSample).count()
    return db.query(PduRollup).filter(PduRollup.bucket_seconds == bucket).count()


def test_prune_keeps_unrolled_data_and_deletes_covered_data(db_session, site, monkeypatch):
    monkeypatch.setattr(settings, "power_history_raw_retention_days", 1)
    monkeypatch.setattr(settings, "power_history_5m_retention_days", 2)
    monkeypatch.setattr(settings, "power_history_1h_retention_days", 3)
    pdu = make_pdu(db_session, site)
    put_raw(db_session, pdu.id, NOW - timedelta(days=4), 4 * 24 * 60)  # 4 days of minutes

    assert history.prune(db_session, NOW)["pdu"] == {"raw": 0, "5m": 0, "1h": 0}  # nothing rolled up yet
    assert count(db_session) == 4 * 24 * 60

    history.rollup(db_session, NOW)
    first = history.prune(db_session, NOW)["pdu"]
    assert first["raw"] > 0 and first["5m"] > 0 and first["1h"] > 0
    oldest_raw = db_session.query(PduSample).order_by(PduSample.sampled_at).first().sampled_at
    assert oldest_raw.replace(tzinfo=timezone.utc) >= NOW - timedelta(days=1)
    oldest_5m = rollup_rows(db_session, BUCKET_5M)[0].bucket_start.replace(tzinfo=timezone.utc)
    assert oldest_5m >= NOW - timedelta(days=2)
    oldest_1h = rollup_rows(db_session, BUCKET_1H)[0].bucket_start.replace(tzinfo=timezone.utc)
    assert oldest_1h >= NOW - timedelta(days=3)

    assert history.prune(db_session, NOW)["pdu"] == {"raw": 0, "5m": 0, "1h": 0}  # re-run changes nothing


def test_prune_does_not_outrun_a_stalled_rollup(db_session, site, monkeypatch):
    monkeypatch.setattr(settings, "power_history_raw_retention_days", 1)
    pdu = make_pdu(db_session, site)
    put_raw(db_session, pdu.id, NOW - timedelta(days=3), 3 * 24 * 60)
    history.rollup(db_session, NOW - timedelta(days=2))  # rollups only reach two days ago

    history.prune(db_session, NOW)

    oldest_unrolled = NOW - timedelta(days=2)
    remaining = db_session.query(PduSample).order_by(PduSample.sampled_at).first().sampled_at
    assert remaining.replace(tzinfo=timezone.utc) >= history._floor(oldest_unrolled, 300) - timedelta(minutes=5)


# ---------------------------------------------------------------------------
# Range selection
# ---------------------------------------------------------------------------

def test_choose_resolution_by_range():
    assert history.choose_resolution(1) == "raw"
    assert history.choose_resolution(24) == "raw"
    assert history.choose_resolution(25) == "5m"
    assert history.choose_resolution(7 * 24) == "5m"
    assert history.choose_resolution(35 * 24) == "5m"
    assert history.choose_resolution(35 * 24 + 1) == "1h"
    assert history.choose_resolution(365 * 24) == "1h"


def test_hours_are_clamped_to_the_longest_window():
    assert history.clamp_hours(0) == 1
    assert history.clamp_hours(10**6) == 396 * 24
    assert history.clamp_hours("junk") == 24


def test_raw_is_not_offered_when_raw_retention_is_under_a_day(monkeypatch):
    monkeypatch.setattr(settings, "power_history_raw_retention_days", 0)
    assert history.choose_resolution(6) == "5m"


def test_history_reads_the_chosen_tier_and_fills_the_tail_from_finer_data(db_session, site):
    pdu = make_pdu(db_session, site)
    put_raw(db_session, pdu.id, START, 190)  # 06:00 to 09:09
    history.rollup(db_session, NOW)  # rolled up to 09:05, raw rows for 09:05 .. 09:09 are not

    day = history.pdu_history(db_session, pdu, 24, NOW)
    assert day["resolution"] == "raw" and day["bucket_seconds"] == 300
    week = history.pdu_history(db_session, pdu, 7 * 24, NOW)
    assert week["resolution"] == "5m"
    samples = week["metrics"]["power_w"]
    first_ms = int(START.timestamp() * 1000)
    assert first_ms - week["bucket_seconds"] * 1000 < samples[0]["t"] <= first_ms
    # the newest points come from raw rows that are not in a rollup yet
    win = history._window(7 * 24, NOW)
    last = history.read_points(db_session, "pdu", win, {"pdu_id": pdu.id})[(pdu.id,)][-1]
    assert last["w"] == 0 and last["t"] == START + timedelta(minutes=187)
    year = history.pdu_history(db_session, pdu, 365 * 24, NOW)
    assert year["resolution"] == "1h" and year["metrics"]["power_w"]
    win = history._window(365 * 24, NOW)
    points = history.read_points(db_session, "pdu", win, {"pdu_id": pdu.id})[(pdu.id,)]
    assert [p["w"] for p in points][:3] == [3600, 3600, 3600] and points[-1]["w"] == 0


def test_bucketed_values_carry_min_and_max(db_session, site):
    pdu = make_pdu(db_session, site)
    put_raw(db_session, pdu.id, START, 60)
    out = history.pdu_history(db_session, pdu, 3, START + timedelta(hours=1))
    first = out["metrics"]["power_w"][0]
    assert first["min"] <= first["value"] <= first["max"]
    assert out["resolution"] == "raw"


# ---------------------------------------------------------------------------
# Energy
# ---------------------------------------------------------------------------

def _p(minute, power, n=1, w=0, e=None):
    return {"t": START + timedelta(minutes=minute), "n": n, "w": w, "m": {"power_w": (power, power, power)}, "e": e, "x": {}}


def test_energy_from_counter():
    points = [_p(0, 1000, e=(100.0, 100.0)), _p(1, 1000, e=(100.2, 100.2)), _p(2, 1000, e=(100.5, 100.5))]
    assert history.energy_kwh(points) == {"kwh": 0.5, "method": "counter"}


def test_energy_counter_reset_counts_the_new_value():
    points = [_p(0, 0, e=(500.0, 500.0)), _p(1, 0, e=(500.4, 500.4)), _p(2, 0, e=(0.1, 0.1)), _p(3, 0, e=(0.3, 0.3))]
    assert history.energy_kwh(points)["kwh"] == pytest.approx(0.4 + 0.1 + 0.2)


def test_energy_counter_inside_rollup_buckets():
    points = [_p(0, 0, n=5, w=300, e=(10.0, 10.4)), _p(5, 0, n=5, w=300, e=(10.5, 10.9))]
    assert history.energy_kwh(points) == {"kwh": pytest.approx(0.9), "method": "counter"}


def test_energy_integrated_from_power_when_there_is_no_counter(monkeypatch):
    monkeypatch.setattr(settings, "power_history_sample_seconds", 60)
    points = [_p(m, 1200.0) for m in range(60)]  # 1.2 kW for an hour of minute samples
    assert history.energy_kwh(points) == {"kwh": pytest.approx(1.2), "method": "integrated"}
    rolled = [_p(0, 1200.0, n=5, w=300)] * 12
    assert history.energy_kwh(rolled)["kwh"] == pytest.approx(1.2)


def test_energy_is_none_without_data():
    assert history.energy_kwh([]) == {"kwh": None, "method": None}


def test_energy_gap_is_not_counted_when_integrating():
    points = [_p(0, 1200.0), _p(1, 1200.0), _p(600, 1200.0)]
    assert history.energy_kwh(points)["kwh"] == pytest.approx(3 * 1200.0 * 60 / 3_600_000)


# ---------------------------------------------------------------------------
# Scopes
# ---------------------------------------------------------------------------

def test_pdu_history_has_inlets_and_banks_with_rated_current(db_session, site):
    pdu = make_pdu(db_session, site)
    power_service.apply_readings(db_session, pdu, readings(energy=10.0), now=NOW - timedelta(minutes=2))
    power_service.apply_readings(db_session, pdu, readings(energy=10.5), now=NOW - timedelta(minutes=1))
    out = history.pdu_history(db_session, pdu, 24, NOW)
    assert out["energy"] == {"kwh": 0.5, "method": "counter"}
    assert out["inlets"][0]["metrics"]["power_w"][0]["value"] == 1000.0
    banks = {b["name"]: b for b in out["banks"]}
    assert banks["A"]["rated_current_a"] == 16.0
    assert banks["A"]["metrics"]["current_a"][0]["value"] == 4.0
    assert banks["B"]["rated_current_a"] is None


def test_rack_location_and_overall_sum_their_pdus(db_session, site):
    a = make_pdu(db_session, site, "pdu-a")
    b = make_pdu(db_session, site, "pdu-b")
    for pdu, power in ((a, 1000.0), (b, 500.0)):
        power_service.apply_readings(db_session, pdu, readings(power), now=NOW - timedelta(minutes=1))
    loc, rack = site
    rack_out = history.rack_history(db_session, rack.id, rack.name, 24, NOW)
    assert rack_out["metrics"]["power_w"][0]["value"] == 1500.0
    assert rack_out["pdu_count"] == 2 and rack_out["pdus_with_data"] == 2
    assert rack_out["current_power_w"] == 1500.0
    assert history.location_history(db_session, loc.id, loc.name, 24, NOW)["metrics"]["power_w"][0]["value"] == 1500.0
    overall = history.location_history(db_session, None, None, 24, NOW)
    assert overall["scope"] == "all" and overall["metrics"]["power_w"][0]["value"] == 1500.0
    assert overall["energy"]["method"] == "integrated"


def test_feed_history_sums_inlets_and_reports_limits(db_session, site):
    loc, rack = site
    feed = PowerFeed(rack_id=rack.id, name="A1", voltage=208, amperage=30, phase="single", max_utilisation_percent=80)
    db_session.add(feed)
    db_session.commit()
    a = make_pdu(db_session, site, "pdu-a")
    b = make_pdu(db_session, site, "pdu-b")
    for pdu in (a, b):
        pdu.inlets[0].power_feed_id = feed.id
    db_session.commit()
    for pdu, power in ((a, 1000.0), (b, 600.0)):
        power_service.apply_readings(db_session, pdu, readings(power), now=NOW - timedelta(minutes=1))
    db_session.refresh(feed)
    out = history.feed_history(db_session, feed, 24, NOW)
    assert out["metrics"]["power_w"][0]["value"] == 1600.0
    assert out["limits"]["usable_current_a"] == 24.0 and out["limits"]["amperage"] == 30
    assert {i["pdu_name"] for i in out["inlets"]} == {"pdu-a", "pdu-b"}


def test_empty_scope_returns_empty_series(db_session, site):
    loc, rack = site
    out = history.rack_history(db_session, rack.id, rack.name, 24, NOW)
    assert out["metrics"]["power_w"] == [] and out["energy"] == {"kwh": None, "method": None}
    assert out["pdu_count"] == 0


def test_totals_mark_buckets_where_a_pdu_had_no_data(db_session, site):
    a = make_pdu(db_session, site, "pdu-a")
    b = make_pdu(db_session, site, "pdu-b")
    put_raw(db_session, a.id, START, 60, power=lambda m: 100.0)
    put_raw(db_session, b.id, START, 30, power=lambda m: 50.0)  # b goes quiet after 30 minutes
    loc, rack = site
    out = history.rack_history(db_session, rack.id, rack.name, 3, START + timedelta(hours=1))
    samples = out["metrics"]["power_w"]
    full = [x for x in samples if x["value"] == 150.0]
    dipped = [x for x in samples if x["value"] == 100.0]
    assert full and dipped
    assert not any(x.get("partial") for x in full)
    assert all(x.get("partial") is True for x in dipped)
    assert out["partial"] is True


def test_totals_are_not_partial_when_every_pdu_reports(db_session, site):
    a = make_pdu(db_session, site, "pdu-a")
    b = make_pdu(db_session, site, "pdu-b")
    for pdu in (a, b):
        put_raw(db_session, pdu.id, START, 30, power=lambda m: 100.0)
    loc, rack = site
    out = history.location_history(db_session, loc.id, loc.name, 3, START + timedelta(minutes=30))
    assert out["partial"] is False and not any("partial" in x for x in out["metrics"]["power_w"])


def test_a_pdu_with_no_data_at_all_makes_the_total_partial(db_session, site):
    a = make_pdu(db_session, site, "pdu-a")
    make_pdu(db_session, site, "pdu-b")
    put_raw(db_session, a.id, START, 30, power=lambda m: 100.0)
    out = history.location_history(db_session, None, None, 3, START + timedelta(minutes=30))
    assert out["partial"] is True and out["pdus_with_data"] == 1


def test_a_disabled_pdu_does_not_make_the_total_partial(db_session, site):
    a = make_pdu(db_session, site, "pdu-a")
    b = make_pdu(db_session, site, "pdu-b")
    b.enabled = False
    db_session.commit()
    put_raw(db_session, a.id, START, 30, power=lambda m: 100.0)
    out = history.location_history(db_session, None, None, 3, START + timedelta(minutes=30))
    assert out["partial"] is False
