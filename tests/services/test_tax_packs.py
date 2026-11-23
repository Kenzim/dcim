"""Tax rule packs: data, schema, registry and importer (docs/billing-phase2-design.md 7)."""

from __future__ import annotations

import copy
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import event, func, select, update
from sqlalchemy.dialects import mysql

from app.models.commerce_audit import UserAuditEvent
from app.models.tax import TaxClass, TaxLegalNote, TaxRule
from app.plugin_host import providers as plugin_providers
from app.plugin_host.providers import ProviderRef, ProviderUnavailable
from app.services.tax.id_formats import EU_MEMBER_STATES
from app.services.tax import pack_import
from app.services.tax.pack_import import PackImporter, PackImportError
from app.services.tax.packs import registry as registry_module
from app.services.tax.packs.registry import PackNotFound, PackRegistry, PackUnavailable
from app.services.tax.packs.schema import PackFormatError, expand_rules, validate_pack
from tests.services.tax_pack_helpers import EFFECTIVE, NOW, SEEDED_NOTES, apply_pack, seed_tax_basics

PACK_DIR = Path(registry_module.__file__).resolve().parent
UK = json.loads((PACK_DIR / "uk_vat.json").read_text())
EU = json.loads((PACK_DIR / "eu_vat.json").read_text())

# Standard rates checked on 2026-11-22 against the European Commission's Your Europe
# table (page last checked 13 July 2026) and GOV.UK; docs/billing-tax-packs.md lists the sources.
EXPECTED_STANDARD = {
    "AT": "20", "BE": "21", "BG": "20", "CY": "19", "CZ": "21", "DE": "19", "DK": "25",
    "EE": "24", "ES": "21", "FI": "25.5", "FR": "20", "GR": "24", "HR": "25", "HU": "27",
    "IE": "23", "IT": "22", "LT": "21", "LU": "17", "LV": "21", "MT": "18", "NL": "21",
    "PL": "23", "PT": "23", "RO": "21", "SE": "25", "SI": "22", "SK": "23",
}


@pytest.fixture(autouse=True)
def _no_plugins(monkeypatch):
    monkeypatch.setattr(plugin_providers, "providers_of", lambda kind: [])
    registry_module._builtin.cache_clear()
    yield
    getattr(registry_module._builtin, "cache_clear", lambda: None)()


@pytest.fixture
def basics(db_session):
    seed_tax_basics(db_session)
    return db_session


# --- pack data -----------------------------------------------------------------------


def test_both_packs_validate_and_declare_their_id():
    assert validate_pack(UK).pack_id == "uk_vat"
    assert validate_pack(EU).pack_id == "eu_vat"


def test_eu_pack_covers_the_27_member_states_with_the_checked_standard_rates():
    assert set(EXPECTED_STANDARD) == EU_MEMBER_STATES
    rules = {rule["key"]: rule for rule in EU["rules"]}
    for state, rate in EXPECTED_STANDARD.items():
        expected = Decimal(rate)
        for cls in ("standard", "digital_service"):
            for suffix, jurisdiction, country in (
                ("domestic", state, state),
                ("oss", "EU_OSS", state),
                ("seller_state", state, None),
            ):
                rule = rules[f"eu_{state}_{cls}_{suffix}"]
                assert Decimal(rule["rate_percent"]) == expected, rule["key"]
                assert (rule["jurisdiction"], rule["country"], rule["class"]) == (jurisdiction, country, cls)
                assert rule["customer_type"] == "any"
                assert rule["name"] == f"{state} VAT {rate}%"
                assert (rule["rounding_point"], rule["rounding_rule"]) == ("invoice_rate", "round_half_up")
                assert rule["valid_to"] is None
                assert not rule.get("reverse_charge")
    assert set(EU["jurisdictions"]) == EU_MEMBER_STATES | {"EU_OSS"}


def test_greece_uses_its_iso_code_in_rules():
    keys = {rule["key"] for rule in EU["rules"]}
    assert "eu_GR_standard_oss" in keys
    assert not any("_EL_" in key for key in keys)


def test_uk_pack_rates():
    rules = {rule["key"]: rule for rule in UK["rules"]}
    assert {k: rules[k]["rate_percent"] for k in ("gb_standard", "gb_digital_service", "gb_reduced", "gb_zero")} == {
        "gb_standard": "20.0000",
        "gb_digital_service": "20.0000",
        "gb_reduced": "5.0000",
        "gb_zero": "0.0000",
    }
    assert all(rule["jurisdiction"] == "GB" for rule in UK["rules"])
    assert all(rule["country"] in ("GB", None) for rule in UK["rules"])
    assert not any(rule.get("country") == "XI" for rule in UK["rules"])


def test_reverse_charge_rows_are_zero_rated_business_only_with_notes():
    for pack in (UK, EU):
        rc = [rule for rule in pack["rules"] if rule.get("reverse_charge")]
        assert rc
        for rule in rc:
            assert rule["rate_percent"] == "0.0000"
            assert rule["customer_type"] == "business"
            assert rule["country"] is None
            assert rule["legal_note_key"] in {"gb_outside_scope_b2b", "eu_reverse_charge", "outside_scope"}
            if rule["reverse_charge_zone"] == "EU":
                assert rule["priority"] > 0
    uk_notes = {r["legal_note_key"] for r in UK["rules"] if r.get("reverse_charge")}
    eu_notes = {r["legal_note_key"] for r in EU["rules"] if r.get("reverse_charge")}
    assert uk_notes == {"gb_outside_scope_b2b", "outside_scope"}
    assert eu_notes == {"eu_reverse_charge", "outside_scope"}


def test_no_uk_domestic_reverse_charge():
    """Owner question 16: hosting is not under the UK domestic reverse charge."""
    for rule in UK["rules"]:
        if rule.get("reverse_charge"):
            assert rule["reverse_charge_zone"] in ("EU", None)
            assert rule["country"] is None


def test_pack_note_texts_equal_the_seeded_texts():
    spec = importlib.util.spec_from_file_location(
        "seed", Path(__file__).resolve().parents[2] / "alembic/versions/billing_p2_tax_seed.py"
    )
    seed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed)
    seeded = dict(seed.LEGAL_NOTES)
    assert seeded == SEEDED_NOTES
    for pack in (UK, EU):
        for key, texts in pack["legal_notes"].items():
            assert texts["en"] == seeded[key]


def test_packs_cite_sources_and_say_they_are_not_advice():
    for pack in (UK, EU):
        meta = pack["metadata"]
        assert meta["rates_checked_on"] == "2026-11-22"
        assert meta["sources"] and all(s["url"].startswith("https://") for s in meta["sources"])
        assert "not tax advice" in meta["description"]
        assert meta["unconfirmed"]


def test_expand_any_eu_into_one_row_per_member_state():
    pack = validate_pack(
        {
            "pack_id": "x",
            "version": "1",
            "jurisdictions": sorted(EU_MEMBER_STATES),
            "classes": ["standard"],
            "legal_notes": {"eu_reverse_charge": {"en": "RC"}},
            "rules": [
                {
                    "key": "rc", "name": "RC", "country": None, "class": "standard", "customer_type": "business",
                    "rate_percent": "0", "jurisdiction": "ANY_EU", "reverse_charge": True,
                    "reverse_charge_zone": "EU", "legal_note_key": "eu_reverse_charge",
                    "rounding_point": "invoice_rate", "rounding_rule": "round_half_up", "valid_from": "2021-01-01",
                }
            ],
        }
    )
    rows = expand_rules(pack)
    assert len(rows) == 27
    assert {row["jurisdiction"] for row in rows} == EU_MEMBER_STATES
    assert {row["pack_rule_key"] for row in rows} == {f"rc_{s}" for s in EU_MEMBER_STATES}
    assert all(row["rate_percent"] == "0.0000" for row in rows)


# --- schema refusals -----------------------------------------------------------------


def _mutated(mutate):
    data = copy.deepcopy(UK)
    mutate(data)
    return data


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(extra_field=1),
        lambda d: d["rules"][0].update(rate_percent=20),
        lambda d: d["rules"][0].update(rate_percent="20.00001"),
        lambda d: d["rules"][0].update(rate_percent="101"),
        lambda d: d["rules"][0].update(rate_percent="-1"),
        lambda d: d["rules"][0].update(country="gb"),
        lambda d: d["rules"][0].update(country="XI"),
        lambda d: d["rules"][0].update(country="IM"),
        lambda d: d["rules"][0].update(country="MC"),
        lambda d: (d["jurisdictions"].append("EL"), d["rules"][0].update(jurisdiction="EL")),
        lambda d: (d["jurisdictions"].append("UK"), d["rules"][0].update(jurisdiction="UK")),
        lambda d: (d["jurisdictions"].append("EU"), d["rules"][0].update(jurisdiction="EU")),
        lambda d: d["jurisdictions"].append("EL"),
        lambda d: d["jurisdictions"].append("UK"),
        lambda d: d["jurisdictions"].append("EU"),
        lambda d: d["rules"][0].update(rounding_rule="round_nearest"),
        lambda d: d["rules"][0].update(rounding_point="invoice"),
        lambda d: d["rules"][0].update(jurisdiction="US"),
        lambda d: d["rules"][0].update(customer_type="person"),
        lambda d: d["rules"][0].update(valid_from="2026-13-01"),
        lambda d: d["rules"][0].update(valid_to="2000-01-01"),
        lambda d: d["rules"][0].update(reverse_charge_zone="EU"),
        lambda d: d["rules"][0].update(legal_note_key="missing_note"),
        lambda d: d["rules"][0].update(**{"class": "unknown_class"}),
        lambda d: d["rules"][0].update(unknown=True),
        lambda d: d["rules"].append(copy.deepcopy(d["rules"][0])),
        lambda d: d["rules"][4].update(rate_percent="1.0000"),
        lambda d: d["rules"][4].update(customer_type="any"),
        lambda d: d["rules"][4].update(legal_note_key=None),
        lambda d: d["rules"][4].update(jurisdiction="ANY"),
        lambda d: d["legal_notes"].update(bad={"en": ""}),
        lambda d: d["legal_notes"].update(bad={"english": "x"}),
        lambda d: d.update(jurisdictions=[]),
    ],
)
def test_schema_refuses(mutate):
    assert UK["rules"][4].get("reverse_charge") is True
    with pytest.raises(PackFormatError):
        validate_pack(_mutated(mutate))


def test_schema_refuses_a_non_object():
    with pytest.raises(PackFormatError):
        validate_pack([])


# --- registry ------------------------------------------------------------------------


def test_registry_lists_the_builtin_packs(db_session):
    infos = PackRegistry.list(db_session)
    assert [(i.pack_id, i.version, i.source, i.available) for i in infos] == [
        ("uk_vat", UK["version"], "builtin", True),
        ("eu_vat", EU["version"], "builtin", True),
    ]


def test_registry_load_builtin_and_wrong_version(db_session):
    data = PackRegistry.load(db_session, "eu_vat", EU["version"])
    # 27 states x 2 classes x 3 rows, plus 4 ANY_EU rules x 27 states.
    assert len(data.rules) == 27 * 6 + 4 * 27
    assert {n["key"] for n in data.legal_notes} == {"eu_reverse_charge", "outside_scope"}
    with pytest.raises(PackNotFound):
        PackRegistry.load(db_session, "eu_vat", "1999.1.1")
    with pytest.raises(PackNotFound):
        PackRegistry.load(db_session, "nope", "1")


def test_registry_survives_a_broken_plugin_host(db_session, monkeypatch):
    def boom(kind):
        raise RuntimeError("host down")

    monkeypatch.setattr(plugin_providers, "providers_of", boom)
    assert [i.pack_id for i in PackRegistry.list(db_session)] == ["uk_vat", "eu_vat"]


def _plugin_pack(version="1.0"):
    data = copy.deepcopy(UK)
    data["pack_id"] = "acme:ca_gst"
    data["version"] = version
    return data


@pytest.fixture
def plugin_pack(monkeypatch):
    calls = []
    state = {"fail": None, "data": _plugin_pack()}

    def providers_of(kind):
        assert kind == "tax_rule_pack"
        return [ProviderRef("acme", "ca_gst", kind)]

    def call_provider(kind, ref, method, *args):
        calls.append((kind, ref, method, args))
        if state["fail"]:
            raise ProviderUnavailable(state["fail"])
        if method == "versions":
            return ["0.9", state["data"]["version"]]
        if method == "load":
            return copy.deepcopy(state["data"])
        raise AssertionError(method)

    monkeypatch.setattr(plugin_providers, "providers_of", providers_of)
    monkeypatch.setattr(plugin_providers, "call_provider", call_provider)
    return state, calls


def test_registry_lists_and_loads_a_plugin_pack(db_session, plugin_pack):
    state, calls = plugin_pack
    infos = PackRegistry.list(db_session)
    assert infos[-1].pack_id == "acme:ca_gst" and infos[-1].version == "1.0" and infos[-1].source == "plugin"
    data = PackRegistry.load(db_session, "acme:ca_gst", "1.0")
    assert data.extra["source"] == "plugin"
    assert ("tax_rule_pack", "acme:ca_gst", "load", ("1.0",)) in calls


def test_plugin_pack_failure_is_unavailable_not_an_error(db_session, plugin_pack):
    state, _ = plugin_pack
    state["fail"] = "circuit_open"
    info = PackRegistry.list(db_session)[-1]
    assert (info.available, info.error) == (False, "circuit_open")
    with pytest.raises(PackUnavailable):
        PackRegistry.load(db_session, "acme:ca_gst", "1.0")


def test_invalid_plugin_pack_data_is_unavailable(db_session, plugin_pack):
    state, _ = plugin_pack
    state["data"]["rules"][0]["rate_percent"] = "abc"
    with pytest.raises(PackUnavailable):
        PackRegistry.load(db_session, "acme:ca_gst", "1.0")


# --- importer ------------------------------------------------------------------------


def _rules(db, pack_id="uk_vat"):
    return db.scalars(select(TaxRule).where(TaxRule.pack_id == pack_id).order_by(TaxRule.id)).all()


def _aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def test_first_preview_adds_everything_and_writes_nothing(basics):
    diff = PackImporter.preview(basics, "uk_vat", UK["version"], EFFECTIVE, now=NOW)
    rules = [e for e in diff.entries if e["type"] == "rule"]
    assert len(rules) == len(UK["rules"]) and all(e["action"] == "add" for e in rules)
    assert [e["key"] for e in diff.entries if e["type"] == "tax_class"] == ["digital_service"]
    assert {e["action"] for e in diff.entries if e["type"] == "legal_note"} == {"unchanged"}
    assert len(diff.diff_hash) == 64
    assert _rules(basics) == []
    assert basics.scalar(select(TaxClass).where(TaxClass.code == "digital_service")) is None


def test_apply_inserts_rules_from_effective_from_and_audits(basics, test_admin_user):
    diff = apply_pack(basics, "uk_vat", actor_user_id=test_admin_user.id)
    rows = _rules(basics)
    assert len(rows) == len(UK["rules"])
    for row in rows:
        assert _aware(row.valid_from) == EFFECTIVE
        assert row.valid_to is None and row.enabled
        assert (row.source.value if hasattr(row.source, "value") else row.source) == "pack"
        assert row.pack_version == UK["version"]
    std = next(r for r in rows if r.pack_rule_key == "gb_standard")
    assert std.rate_percent == Decimal("20.0000") and std.jurisdiction == "GB" and std.country == "GB"
    rc = next(r for r in rows if r.pack_rule_key == "gb_b2b_eu_reverse_charge_digital_service")
    assert rc.reverse_charge and rc.reverse_charge_zone == "EU" and rc.legal_note_key == "gb_outside_scope_b2b"
    digital = basics.scalar(select(TaxClass).where(TaxClass.code == "digital_service"))
    assert digital is not None and not digital.system
    audit = basics.scalars(select(UserAuditEvent).where(UserAuditEvent.action == "billing.tax.pack_applied")).one()
    assert audit.actor_user_id == test_admin_user.id and audit.resource_id == "uk_vat"
    assert audit.event_metadata["diff_hash"] == diff.diff_hash
    assert audit.event_metadata["counts"]["add"] == len(UK["rules"]) + 1


def test_reapply_is_unchanged(basics):
    apply_pack(basics, "uk_vat")
    later = EFFECTIVE + timedelta(days=30)
    diff = PackImporter.preview(basics, "uk_vat", UK["version"], later, now=NOW)
    assert {e["action"] for e in diff.entries} == {"unchanged"}
    PackImporter.apply(basics, "uk_vat", UK["version"], later, diff_hash=diff.diff_hash, actor_user_id=None, now=NOW)
    assert len(_rules(basics)) == len(UK["rules"])


def test_past_effective_from_is_refused(basics):
    with pytest.raises(PackImportError) as err:
        PackImporter.preview(basics, "uk_vat", UK["version"], NOW - timedelta(seconds=1), now=NOW)
    assert err.value.code == "effective_from_in_past"
    with pytest.raises(PackImportError) as err:
        PackImporter.apply(
            basics, "uk_vat", UK["version"], NOW - timedelta(days=1), diff_hash="x", actor_user_id=None, now=NOW
        )
    assert err.value.code == "effective_from_in_past"
    assert _rules(basics) == []


def test_naive_effective_from_is_read_as_utc(basics):
    diff = PackImporter.preview(basics, "uk_vat", UK["version"], datetime(2026, 12, 1), now=NOW)
    assert diff.effective_from == EFFECTIVE


def test_wrong_hash_is_pack_changed(basics):
    with pytest.raises(PackImportError) as err:
        PackImporter.apply(basics, "uk_vat", UK["version"], EFFECTIVE, diff_hash="0" * 64, actor_user_id=None, now=NOW)
    assert err.value.code == "pack_changed"
    assert _rules(basics) == []


def test_hash_changes_when_applied_rules_change_after_preview(basics):
    diff = PackImporter.preview(basics, "uk_vat", UK["version"], EFFECTIVE, now=NOW)
    apply_pack(basics, "uk_vat")
    with pytest.raises(PackImportError) as err:
        PackImporter.apply(
            basics, "uk_vat", UK["version"], EFFECTIVE, diff_hash=diff.diff_hash, actor_user_id=None, now=NOW
        )
    assert err.value.code == "pack_changed"


def _statements(db):
    """Record, in order, each ORM select of tax rules and each SQL write to tax_rules."""
    seen = []

    def on_orm(state):
        if state.is_select and any(d.get("entity") is TaxRule for d in state.statement.column_descriptions):
            seen.append(("lock" if state.statement._for_update_arg is not None else "read", state.statement))

    def on_sql(conn, cursor, statement, params, context, executemany):
        if statement.lstrip().startswith(("UPDATE tax_rules", "INSERT INTO tax_rules")):
            seen.append(("write", statement))

    event.listen(db, "do_orm_execute", on_orm)
    event.listen(db.get_bind(), "before_cursor_execute", on_sql)
    return seen, lambda: (event.remove(db, "do_orm_execute", on_orm), event.remove(db.get_bind(), "before_cursor_execute", on_sql))


def _bump_nonzero_rates(data):
    for rule in data["rules"]:
        if Decimal(rule["rate_percent"]) > 0:
            rule["rate_percent"] = str(Decimal(rule["rate_percent"]) + 1)


CHANGE_AT = datetime(2027, 4, 1, tzinfo=timezone.utc)
CHANGE_NOW = datetime(2027, 1, 10, tzinfo=timezone.utc)


def test_apply_locks_the_rules_in_effect_in_one_id_ordered_statement_before_writing(basics, monkeypatch):
    apply_pack(basics, "uk_vat")
    version = _with_new_version(monkeypatch, _bump_nonzero_rates)
    seen, stop = _statements(basics)
    try:
        diff = PackImporter.preview(basics, "uk_vat", version, CHANGE_AT, now=CHANGE_NOW)
        assert [kind for kind, _ in seen] == ["read"]  # a preview takes no locks
        seen.clear()
        PackImporter.apply(basics, "uk_vat", version, CHANGE_AT, diff_hash=diff.diff_hash, actor_user_id=None, now=CHANGE_NOW)
    finally:
        stop()
    kinds = [kind for kind, _ in seen]
    assert kinds[0] == "lock" and kinds.count("lock") == 1 and "read" not in kinds
    assert {"write"} == set(kinds[1:])
    sql = str(seen[0][1].compile(dialect=mysql.dialect()))
    assert "FROM tax_rules FORCE INDEX (PRIMARY)" in sql
    assert sql.rstrip().endswith("ORDER BY tax_rules.id FOR UPDATE")
    assert "tax_rules.pack_id = %s" in sql


def test_apply_refuses_when_a_rule_changed_after_the_preview_and_writes_nothing(basics, monkeypatch):
    apply_pack(basics, "uk_vat")
    basics.commit()
    version = _with_new_version(monkeypatch, _bump_nonzero_rates)
    diff = PackImporter.preview(basics, "uk_vat", version, CHANGE_AT, now=CHANGE_NOW)
    before = [(r.id, r.rate_percent, r.valid_to) for r in _rules(basics)]
    audits = basics.scalar(select(func.count()).select_from(UserAuditEvent))
    # Another administrator ends a rule between the preview and the apply.
    basics.execute(update(TaxRule).where(TaxRule.pack_rule_key == "gb_zero").values(valid_to=CHANGE_AT + timedelta(days=30)))
    with pytest.raises(PackImportError) as err:
        PackImporter.apply(basics, "uk_vat", version, CHANGE_AT, diff_hash=diff.diff_hash, actor_user_id=None, now=CHANGE_NOW)
    assert err.value.code == "pack_changed" and "preview the pack again" in str(err.value)
    basics.rollback()
    assert [(r.id, r.rate_percent, r.valid_to) for r in _rules(basics)] == before
    assert basics.scalar(select(func.count()).select_from(UserAuditEvent)) == audits


def test_an_apply_that_fails_part_way_leaves_nothing_once_rolled_back(basics, monkeypatch):
    apply_pack(basics, "uk_vat")
    basics.commit()
    before = [(r.id, r.rate_percent, r.valid_to) for r in _rules(basics)]
    audits = basics.scalar(select(func.count()).select_from(UserAuditEvent))
    version = _with_new_version(monkeypatch, _bump_nonzero_rates)
    diff = PackImporter.preview(basics, "uk_vat", version, CHANGE_AT, now=CHANGE_NOW)
    real_insert, calls = pack_import._insert, []

    def failing_insert(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            basics.flush()  # the first rule is closed and superseded in the database
            raise RuntimeError("Deadlock found when trying to get lock (1213)")
        return real_insert(*args, **kwargs)

    monkeypatch.setattr(pack_import, "_insert", failing_insert)
    with pytest.raises(RuntimeError):
        PackImporter.apply(basics, "uk_vat", version, CHANGE_AT, diff_hash=diff.diff_hash, actor_user_id=None, now=CHANGE_NOW)
    basics.rollback()
    assert [(r.id, r.rate_percent, r.valid_to) for r in _rules(basics)] == before
    assert basics.scalar(select(func.count()).select_from(UserAuditEvent)) == audits


def test_unknown_pack_and_version(basics):
    with pytest.raises(PackImportError) as err:
        PackImporter.preview(basics, "nope", "1", EFFECTIVE, now=NOW)
    assert err.value.code == "pack_not_found"
    with pytest.raises(PackImportError):
        PackImporter.preview(basics, "uk_vat", "0.0.1", EFFECTIVE, now=NOW)


def _with_new_version(monkeypatch, mutate, version="2027.1.1"):
    data = copy.deepcopy(UK)
    data["version"] = version
    mutate(data)
    original = registry_module._builtin

    def builtin(pack_id):
        if pack_id == "uk_vat":
            return registry_module._pack_data("uk_vat", validate_pack(data), "builtin")
        return original(pack_id)

    monkeypatch.setattr(registry_module, "_builtin", builtin)
    return version


def test_rate_change_supersedes_from_effective_from(basics, monkeypatch):
    apply_pack(basics, "uk_vat")
    old = next(r for r in _rules(basics) if r.pack_rule_key == "gb_standard")

    def bump(data):
        next(r for r in data["rules"] if r["key"] == "gb_standard")["rate_percent"] = "21.0000"

    version = _with_new_version(monkeypatch, bump)
    when = datetime(2027, 4, 1, tzinfo=timezone.utc)
    later_now = datetime(2027, 1, 10, tzinfo=timezone.utc)
    diff = PackImporter.preview(basics, "uk_vat", version, when, now=later_now)
    change = [e for e in diff.entries if e["action"] == "change"]
    assert [e["key"] for e in change] == ["gb_standard"]
    assert change[0]["old"]["rate_percent"] == "20.0000" and change[0]["new"]["rate_percent"] == "21.0000"
    assert diff.count("unchanged") == len(UK["rules"]) - 1 + 2  # the rules and two legal notes
    PackImporter.apply(basics, "uk_vat", version, when, diff_hash=diff.diff_hash, actor_user_id=None, now=later_now)

    basics.refresh(old)
    assert _aware(old.valid_to) == when and old.rate_percent == Decimal("20.0000")
    rows = [r for r in _rules(basics) if r.pack_rule_key == "gb_standard"]
    assert len(rows) == 2
    new = rows[-1]
    assert new.supersedes_id == old.id
    assert _aware(new.valid_from) == when and new.valid_to is None
    assert new.rate_percent == Decimal("21.0000") and new.pack_version == version
    # Untouched rules keep their row and version.
    std_digital = [r for r in _rules(basics) if r.pack_rule_key == "gb_digital_service"]
    assert len(std_digital) == 1 and std_digital[0].pack_version == UK["version"]


def test_superseding_row_keeps_an_admin_disable(basics, monkeypatch):
    apply_pack(basics, "uk_vat")
    old = next(r for r in _rules(basics) if r.pack_rule_key == "gb_reduced")
    old.enabled = False
    basics.flush()

    def bump(data):
        next(r for r in data["rules"] if r["key"] == "gb_reduced")["rate_percent"] = "6.0000"

    version = _with_new_version(monkeypatch, bump)
    when = datetime(2027, 4, 1, tzinfo=timezone.utc)
    diff = PackImporter.preview(basics, "uk_vat", version, when, now=NOW)
    PackImporter.apply(basics, "uk_vat", version, when, diff_hash=diff.diff_hash, actor_user_id=None, now=NOW)
    new = [r for r in _rules(basics) if r.pack_rule_key == "gb_reduced"][-1]
    assert new.supersedes_id == old.id and new.enabled is False


def test_removed_rule_is_closed(basics, monkeypatch):
    apply_pack(basics, "uk_vat")

    def drop(data):
        data["rules"] = [r for r in data["rules"] if r["key"] != "gb_zero"]

    version = _with_new_version(monkeypatch, drop)
    when = datetime(2027, 4, 1, tzinfo=timezone.utc)
    diff = PackImporter.preview(basics, "uk_vat", version, when, now=NOW)
    assert [e["key"] for e in diff.entries if e["action"] == "remove"] == ["gb_zero"]
    PackImporter.apply(basics, "uk_vat", version, when, diff_hash=diff.diff_hash, actor_user_id=None, now=NOW)
    zero = [r for r in _rules(basics) if r.pack_rule_key == "gb_zero"]
    assert len(zero) == 1 and _aware(zero[0].valid_to) == when


def test_pack_rule_with_a_future_end_date(basics, monkeypatch):
    end = "2027-06-01"

    def ending(data):
        next(r for r in data["rules"] if r["key"] == "gb_reduced")["valid_to"] = end

    version = _with_new_version(monkeypatch, ending)
    diff = PackImporter.preview(basics, "uk_vat", version, EFFECTIVE, now=NOW)
    PackImporter.apply(basics, "uk_vat", version, EFFECTIVE, diff_hash=diff.diff_hash, actor_user_id=None, now=NOW)
    reduced = next(r for r in _rules(basics) if r.pack_rule_key == "gb_reduced")
    assert _aware(reduced.valid_to) == datetime(2027, 6, 1, tzinfo=timezone.utc)
    # After the end the rule is no longer in effect: a preview from then has nothing for it.
    after = PackImporter.preview(basics, "uk_vat", version, datetime(2027, 7, 1, tzinfo=timezone.utc), now=NOW)
    assert "gb_reduced" not in {e["key"] for e in after.entries if e["type"] == "rule"}


def test_pack_rule_starting_after_effective_from(basics, monkeypatch):
    def later(data):
        next(r for r in data["rules"] if r["key"] == "gb_reduced")["valid_from"] = "2027-02-01"

    version = _with_new_version(monkeypatch, later)
    diff = PackImporter.preview(basics, "uk_vat", version, EFFECTIVE, now=NOW)
    PackImporter.apply(basics, "uk_vat", version, EFFECTIVE, diff_hash=diff.diff_hash, actor_user_id=None, now=NOW)
    reduced = next(r for r in _rules(basics) if r.pack_rule_key == "gb_reduced")
    assert _aware(reduced.valid_from) == datetime(2027, 2, 1, tzinfo=timezone.utc)


def test_legal_notes_added_updated_and_admin_edits_kept(db_session, monkeypatch):
    db_session.add(TaxClass(code="standard", name="Standard", system=True))
    db_session.add(TaxLegalNote(key="outside_scope", locale="en", text="Edited by staff.", edited_by_admin=True))
    db_session.add(TaxLegalNote(key="gb_outside_scope_b2b", locale="en", text="Old text", edited_by_admin=False))
    db_session.flush()

    def french(data):
        data["legal_notes"]["outside_scope"]["fr"] = "Hors du champ de la TVA."

    version = _with_new_version(monkeypatch, french)
    diff = PackImporter.preview(db_session, "uk_vat", version, EFFECTIVE, now=NOW)
    notes = {(e["key"], e["locale"]): e["action"] for e in diff.entries if e["type"] == "legal_note"}
    assert notes == {
        ("gb_outside_scope_b2b", "en"): "change",
        ("outside_scope", "en"): "kept_edited",
        ("outside_scope", "fr"): "add",
    }
    PackImporter.apply(db_session, "uk_vat", version, EFFECTIVE, diff_hash=diff.diff_hash, actor_user_id=None, now=NOW)
    texts = {(n.key, n.locale): n.text for n in db_session.scalars(select(TaxLegalNote)).all()}
    assert texts[("outside_scope", "en")] == "Edited by staff."
    assert texts[("outside_scope", "fr")] == "Hors du champ de la TVA."
    assert texts[("gb_outside_scope_b2b", "en")] == UK["legal_notes"]["gb_outside_scope_b2b"]["en"]
    # Missing system classes are created as system classes.
    assert db_session.scalar(select(TaxClass).where(TaxClass.code == "zero")).system is True


def test_eu_pack_apply_creates_one_reverse_charge_row_per_state(basics):
    apply_pack(basics, "eu_vat")
    rows = _rules(basics, "eu_vat")
    rc = [r for r in rows if r.reverse_charge and r.reverse_charge_zone == "EU"]
    assert len(rc) == 27 * 2
    assert {r.jurisdiction for r in rc} == EU_MEMBER_STATES
    assert all(r.country is None for r in rc)


def test_both_packs_apply_side_by_side(basics):
    apply_pack(basics, "uk_vat")
    apply_pack(basics, "eu_vat")
    assert len(_rules(basics, "uk_vat")) == len(UK["rules"])
    assert len(_rules(basics, "eu_vat")) == len(PackRegistry.load(basics, "eu_vat", EU["version"]).rules)


def test_rules_not_from_the_pack_are_never_touched(basics):
    standard = basics.scalar(select(TaxClass).where(TaxClass.code == "standard"))
    admin_rule = TaxRule(
        name="Admin GB", country="GB", tax_class_id=standard.id, rate_percent=Decimal("20"), jurisdiction="GB",
        rounding_rule="round_half_up", valid_from=datetime(2020, 1, 1, tzinfo=timezone.utc), source="admin",
    )
    basics.add(admin_rule)
    basics.flush()
    apply_pack(basics, "uk_vat")
    basics.refresh(admin_rule)
    assert admin_rule.valid_to is None and admin_rule.supersedes_id is None


def test_registry_lists_every_plugin_version_oldest_first(db_session, plugin_pack):
    infos = PackRegistry.list(db_session)
    assert [i.versions for i in infos[:2]] == [(UK["version"],), (EU["version"],)]
    assert infos[-1].versions == ("0.9", "1.0") and infos[-1].version == "1.0"
