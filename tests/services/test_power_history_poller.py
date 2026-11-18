"""The poller's PDU history cycle: rollup and pruning from scripts/snmp_bandwidth_poller.py."""
from datetime import datetime, timedelta, timezone

from app.dao.pdu_sample_dao import PduSampleDAO
from app.models.location import Location
from app.models.pdu import Pdu
from app.models.pdu_sample import PduRollup, PduSample
from scripts import snmp_bandwidth_poller as poller


def test_power_history_cycle_rolls_up_and_prunes(db_session, monkeypatch):
    loc = Location(name="DC1", description="")
    db_session.add(loc)
    db_session.flush()
    pdu = Pdu(name="p", location_id=loc.id, plugin_name="x", plugin_config={})
    db_session.add(pdu)
    db_session.commit()
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=9)
    PduSampleDAO.insert_raw(
        db_session,
        "pdu",
        [{"pdu_id": pdu.id, "sampled_at": start + timedelta(minutes=10 * m), "power_w": 500.0} for m in range(9 * 144)],
    )
    db_session.commit()
    monkeypatch.setattr(poller, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)

    result = poller.run_power_history_cycle()

    assert sum(result["rolled_up"].values()) > 0
    assert db_session.query(PduRollup).count() > 0
    oldest_raw = db_session.query(PduSample).order_by(PduSample.sampled_at).first().sampled_at
    assert oldest_raw.replace(tzinfo=timezone.utc) >= now - timedelta(days=7, minutes=5)  # default 7-day window

    again = poller.run_power_history_cycle()
    assert sum(again["rolled_up"].values()) == 0
    assert sum(sum(t.values()) for t in again["deleted"].values()) <= 2
