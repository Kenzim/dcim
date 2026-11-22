"""Tax admin API (design 6.2), the /tax-rates façade and product tax class (design 3)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.commerce_audit import UserAuditEvent
from app.models.commerce_coupon_tax import TaxRate
from app.models.tax import TaxExemption, TaxRule
from app.services.tax.mirror import TaxRuleMirror
from tests.api.test_billing_p1c_engines_api import _admin
from tests.services.tax_helpers import add_rule, retail_account, seed_classes, set_engine

BASE = "/api/admin/billing/tax"


def _later(**delta):
    return (datetime.now(timezone.utc) + timedelta(**delta)).isoformat()


def _actions(db):
    db.expire_all()
    return [row.action for row in db.scalars(select(UserAuditEvent).order_by(UserAuditEvent.id))]


@pytest.fixture
def headers(client, db_session, test_admin_user):
    seed_classes(db_session)
    db_session.commit()
    return _admin(client)


def test_tax_admin_requires_admin(client, test_user):
    assert client.get(f"{BASE}/rules").status_code in (401, 403)


def test_rule_lifecycle_edit_refused_in_use_then_superseded(client, db_session, headers):
    classes = client.get(f"{BASE}/classes", headers=headers).json()
    standard = next(c["id"] for c in classes if c["code"] == "standard")
    future = client.post(
        f"{BASE}/rules", headers=headers,
        json={"name": "VAT", "country": "gb", "tax_class_id": standard, "rate_percent": "20", "valid_from": _later(days=1)},
    )
    assert future.status_code == 201, future.text
    rule_id = future.json()["id"]
    assert client.put(f"{BASE}/rules/{rule_id}", headers=headers, json={"rate_percent": "21"}).status_code == 200

    current = client.post(
        f"{BASE}/rules", headers=headers, json={"name": "Now", "tax_class_id": standard, "rate_percent": "5"}
    ).json()
    resp = client.put(f"{BASE}/rules/{current['id']}", headers=headers, json={"rate_percent": "6"})
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "tax_rule_in_use"
    past = client.post(
        f"{BASE}/rules/{current['id']}/supersede", headers=headers,
        json={"rate_percent": "6", "valid_from": _later(days=-1)},
    )
    assert past.status_code == 422
    new = client.post(
        f"{BASE}/rules/{current['id']}/supersede", headers=headers,
        json={"rate_percent": "6", "valid_from": _later(hours=1)},
    )
    assert new.status_code == 201, new.text
    assert new.json()["supersedes_id"] == current["id"] and new.json()["rate_percent"] == "6.0000"
    old = client.get(f"{BASE}/rules/{current['id']}", headers=headers).json()
    assert old["valid_to"] is not None and old["rate_percent"] == "5.0000"
    assert {"billing.tax.rule.created", "billing.tax.rule.updated", "billing.tax.rule.superseded"} <= set(
        _actions(db_session)
    )


def test_rule_validation(client, headers):
    for body in (
        {"name": "x", "tax_class_id": 1, "rate_percent": "1.00001"},
        {"name": "x", "tax_class_id": 1, "rate_percent": "5", "rounding_rule": "banker"},
        {"name": "x", "tax_class_id": 1, "rate_percent": "5", "postcode_pattern": "("},
    ):
        assert client.post(f"{BASE}/rules", headers=headers, json=body).status_code == 422


def test_superseding_a_mirrored_rule_is_refused(client, db_session, headers):
    """A mirrored rule changes only through /tax-rates, as an edit of it does."""
    rate = TaxRate(name="UK", country="GB", region=None, rate_bps=2000, enabled=True)
    db_session.add(rate)
    db_session.flush()
    rule = TaxRuleMirror.sync(db_session, rate, now=datetime.now(timezone.utc) - timedelta(days=1))
    db_session.commit()
    count = db_session.query(TaxRule).count()
    resp = client.post(
        f"{BASE}/rules/{rule.id}/supersede", headers=headers, json={"rate_percent": "17.5", "valid_from": _later()}
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "tax_rule_mirrored"
    db_session.expire_all()
    kept = db_session.get(TaxRule, rule.id)
    assert kept.legacy_tax_rate_id == rate.id and kept.valid_to is None
    assert db_session.query(TaxRule).count() == count


RULE_NOT_NULL = (
    "name", "tax_class_id", "customer_type", "rate_percent", "priority", "compound", "compound_order",
    "jurisdiction", "reverse_charge", "rounding_point", "rounding_rule", "enabled",
)
REGISTRATION_NOT_NULL = ("jurisdiction", "scheme", "tax_number", "country", "valid_from", "enabled")


def _standard(client, headers):
    return next(c["id"] for c in client.get(f"{BASE}/classes", headers=headers).json() if c["code"] == "standard")


@pytest.mark.parametrize("field", RULE_NOT_NULL)
def test_explicit_null_for_a_not_null_rule_field_is_refused(client, headers, field):
    standard = _standard(client, headers)
    body = {"name": "VAT", "tax_class_id": standard, "rate_percent": "20"}
    created = client.post(f"{BASE}/rules", headers=headers, json={**body, "valid_from": _later(days=1)}).json()
    current = client.post(f"{BASE}/rules", headers=headers, json=body).json()
    assert client.post(f"{BASE}/rules", headers=headers, json={**body, field: None}).status_code == 422
    resp = client.put(f"{BASE}/rules/{created['id']}", headers=headers, json={field: None})
    assert resp.status_code == 422, resp.text
    resp = client.post(
        f"{BASE}/rules/{current['id']}/supersede", headers=headers, json={field: None, "valid_from": _later(hours=1)}
    )
    assert resp.status_code == 422, resp.text
    assert client.get(f"{BASE}/rules/{created['id']}", headers=headers).json()[field] is not None


@pytest.mark.parametrize("field", REGISTRATION_NOT_NULL)
def test_explicit_null_for_a_not_null_registration_field_is_refused(client, headers, field):
    body = {"jurisdiction": "GB", "scheme": "domestic", "tax_number": "GB1", "country": "GB"}
    created = client.post(f"{BASE}/registrations", headers=headers, json=body)
    assert created.status_code == 201, created.text
    resp = client.post(f"{BASE}/registrations", headers=headers, json={**body, field: None})
    assert resp.status_code == 422, resp.text
    resp = client.put(f"{BASE}/registrations/{created.json()['id']}", headers=headers, json={field: None})
    assert resp.status_code == 422, resp.text


def test_explicit_null_class_name_is_refused(client, headers):
    created = client.post(f"{BASE}/classes", headers=headers, json={"code": "books", "name": "Books"}).json()
    resp = client.put(f"{BASE}/classes/{created['id']}", headers=headers, json={"name": None})
    assert resp.status_code == 422, resp.text
    resp = client.put(f"{BASE}/classes/{created['id']}", headers=headers, json={"description": None})
    assert resp.status_code == 200, resp.text


def test_rule_valid_to_must_be_after_valid_from(client, headers):
    """A rule holds on the half-open ``[valid_from, valid_to)``, so an equal ``valid_to`` is empty too."""
    standard = _standard(client, headers)
    body = {"name": "VAT", "tax_class_id": standard, "rate_percent": "20"}
    start = _later(days=1)

    def refused(resp):
        return resp.status_code == 422 and resp.json()["detail"]["code"] == "valid_to_not_after_valid_from"

    assert refused(client.post(f"{BASE}/rules", headers=headers, json={**body, "valid_from": start, "valid_to": start}))
    assert refused(client.post(
        f"{BASE}/rules", headers=headers, json={**body, "valid_from": start, "valid_to": _later(hours=1)}
    ))
    assert refused(client.post(f"{BASE}/rules", headers=headers, json={**body, "valid_to": _later(hours=-1)}))
    future = client.post(f"{BASE}/rules", headers=headers, json={**body, "valid_from": start, "valid_to": _later(days=2)})
    assert future.status_code == 201, future.text
    assert refused(client.put(f"{BASE}/rules/{future.json()['id']}", headers=headers, json={"valid_to": _later(hours=1)}))
    assert client.put(
        f"{BASE}/rules/{future.json()['id']}", headers=headers, json={"valid_to": _later(days=3)}
    ).status_code == 200
    current = client.post(f"{BASE}/rules", headers=headers, json=body).json()
    at = _later(hours=2)
    for valid_to in (at, _later(hours=1)):
        assert refused(client.post(
            f"{BASE}/rules/{current['id']}/supersede", headers=headers, json={"valid_from": at, "valid_to": valid_to}
        ))
    assert client.post(
        f"{BASE}/rules/{current['id']}/supersede", headers=headers, json={"valid_from": at, "valid_to": _later(hours=3)}
    ).status_code == 201


def test_rule_country_xi_is_refused_with_a_pointer_to_gb(client, headers):
    """Northern Ireland is taxed by the GB rules; an XI rule would never match a buyer."""
    standard = _standard(client, headers)
    body = {"name": "VAT", "tax_class_id": standard, "rate_percent": "20"}

    def refused(resp):
        detail = resp.json()["detail"] if resp.status_code == 422 else {}
        return detail.get("code") == "country_xi" and "GB" in detail.get("message", "")

    assert refused(client.post(f"{BASE}/rules", headers=headers, json={**body, "country": "xi"}))
    future = client.post(f"{BASE}/rules", headers=headers, json={**body, "country": "GB", "valid_from": _later(days=1)})
    assert refused(client.put(f"{BASE}/rules/{future.json()['id']}", headers=headers, json={"country": "XI"}))
    current = client.post(f"{BASE}/rules", headers=headers, json={**body, "country": "GB"}).json()
    assert refused(client.post(
        f"{BASE}/rules/{current['id']}/supersede", headers=headers, json={"country": "XI", "valid_from": _later(hours=1)}
    ))
    assert client.get(f"{BASE}/rules/{future.json()['id']}", headers=headers).json()["country"] == "GB"


@pytest.mark.parametrize(("code", "country", "error"), [("IM", "GB", "country_im"), ("MC", "FR", "country_mc")])
def test_rule_country_im_and_mc_are_refused_with_a_pointer(client, headers, code, country, error):
    """The Isle of Man is taxed by GB rules and Monaco by FR rules; a rule for either would never match."""
    standard = _standard(client, headers)
    body = {"name": "VAT", "tax_class_id": standard, "rate_percent": "20"}
    resp = client.post(f"{BASE}/rules", headers=headers, json={**body, "country": code.lower()})
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == error and country in detail["message"]


def test_system_class_cannot_be_deleted_and_used_class_neither(client, db_session, headers):
    classes = {c["code"]: c["id"] for c in client.get(f"{BASE}/classes", headers=headers).json()}
    assert client.delete(f"{BASE}/classes/{classes['standard']}", headers=headers).status_code == 409
    created = client.post(f"{BASE}/classes", headers=headers, json={"code": "books", "name": "Books"}).json()
    add_rule(db_session, tax_class="standard")
    db_session.commit()
    assert client.delete(f"{BASE}/classes/{created['id']}", headers=headers).status_code == 204


def test_exemptions_registrations_and_account_tax(client, db_session, headers):
    _user, account = retail_account(db_session)
    db_session.commit()
    refused = client.post(
        f"{BASE}/exemptions", headers=headers,
        json={"scope": "client_group", "billing_account_id": account.id, "reason": "x"},
    )
    assert refused.status_code == 422
    created = client.post(
        f"{BASE}/exemptions", headers=headers, json={"billing_account_id": account.id, "reason": "charity"}
    )
    assert created.status_code == 201
    revoked = client.post(f"{BASE}/exemptions/{created.json()['id']}/revoke", headers=headers)
    assert revoked.status_code == 200 and revoked.json()["revoked_at"]
    assert db_session.get(TaxExemption, created.json()["id"]) is not None

    reg = client.post(
        f"{BASE}/registrations", headers=headers,
        json={"jurisdiction": "gb", "scheme": "domestic", "tax_number": "GB1", "country": "gb"},
    )
    assert reg.status_code == 201 and reg.json()["jurisdiction"] == "GB"
    assert client.put(
        f"{BASE}/registrations/{reg.json()['id']}", headers=headers, json={"tax_number": "GB2"}
    ).json()["tax_number"] == "GB2"

    assert client.put(
        f"/api/admin/billing/accounts/{account.id}/tax", headers=headers, json={"tax_exempt": True}
    ).status_code == 422
    resp = client.put(
        f"/api/admin/billing/accounts/{account.id}/tax", headers=headers,
        json={"tax_exempt": True, "tax_exempt_reason": "diplomatic"},
    )
    assert resp.json() == {"billing_account_id": account.id, "tax_exempt": True, "tax_exempt_reason": "diplomatic"}
    actions = set(_actions(db_session))
    assert {"billing.tax_exemption.created", "billing.tax_exemption.revoked", "billing.tax.registration.created",
            "billing.tax.registration.updated", "billing.tax.account.updated"} <= actions


def test_preview_answers_without_writing(client, db_session, headers):
    from app.services.settings_store import set_setting

    set_setting(db_session, "billing_tax_enabled", True)
    add_rule(db_session, name="Admin 10%", rate="10", country=None, source="admin")
    db_session.commit()
    before = db_session.scalar(select(TaxRule.id).order_by(TaxRule.id.desc()))
    resp = client.post(
        f"{BASE}/preview", headers=headers,
        json={"buyer": {"country": "US"}, "lines": [{"key": "a", "net_cents": 1000}]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["tax_cents"] == 100 and resp.json()["summary"][0]["rate_percent"] == "10.0000"
    assert db_session.scalar(select(TaxRule.id).order_by(TaxRule.id.desc())) == before


def test_legal_note_edit_is_audited(client, db_session, headers):
    resp = client.put(f"{BASE}/legal-notes", headers=headers, json={"key": "outside_scope", "text": "Outside."})
    assert resp.status_code == 200 and resp.json()["edited_by_admin"] is True
    assert [n["text"] for n in client.get(f"{BASE}/legal-notes", headers=headers).json()
            if n["key"] == "outside_scope"] == ["Outside."]
    assert "billing.tax.legal_note.updated" in _actions(db_session)


# --- /tax-rates façade and product tax class ---


STORE = "/api/admin/store"


def test_tax_rates_routes_write_the_mirrored_rule(client, db_session, headers):
    resp = client.post(
        f"{STORE}/tax-rates", headers=headers, json={"name": "UK", "country": "gb", "rate_bps": 2000}
    )
    assert resp.status_code == 201, resp.text
    assert "Deprecation" not in resp.headers
    rate_id = resp.json()["id"]
    rule = TaxRuleMirror.current(db_session, rate_id)
    assert rule is not None and str(rule.rate_percent).startswith("20")
    resp = client.put(f"{STORE}/tax-rates/{rate_id}", headers=headers, json={"rate_bps": 1750})
    db_session.expire_all()
    new = TaxRuleMirror.current(db_session, rate_id)
    assert new.id != rule.id and new.supersedes_id == rule.id
    set_engine(db_session, "tax", "v2")
    db_session.commit()
    assert client.get(f"{STORE}/tax-rates", headers=headers).headers.get("Deprecation") == "true"


def test_tax_rates_routes_refuse_a_rate_above_100_percent(client, db_session, headers):
    from app.models.commerce_coupon_tax import TaxRate

    resp = client.post(f"{STORE}/tax-rates", headers=headers, json={"name": "X", "country": "FR", "rate_bps": 10_001})
    assert resp.status_code == 422, resp.text
    assert "10000" in resp.text, resp.text
    assert db_session.query(TaxRate).filter_by(country="FR").count() == 0
    resp = client.post(f"{STORE}/tax-rates", headers=headers, json={"name": "Full", "country": "FR", "rate_bps": 10_000})
    assert resp.status_code == 201, resp.text
    rate_id = resp.json()["id"]
    resp = client.put(f"{STORE}/tax-rates/{rate_id}", headers=headers, json={"rate_bps": 10_001})
    assert resp.status_code == 422, resp.text
    assert "10000" in resp.text, resp.text
    db_session.expire_all()
    assert db_session.get(TaxRate, rate_id).rate_bps == 10_000


def test_product_tax_class(client, db_session, headers):
    from tests.services.test_billing_a0_money_path import _product

    product, _plan = _product(db_session)
    db_session.commit()
    classes = {c["code"]: c["id"] for c in client.get(f"{BASE}/classes", headers=headers).json()}
    resp = client.put(
        f"{STORE}/products/{product.id}/tax-class", headers=headers, json={"tax_class_id": classes["reduced"]}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["tax_class_id"] == classes["reduced"]
    assert client.put(
        f"{STORE}/products/{product.id}/tax-class", headers=headers, json={"tax_class_id": 99999}
    ).status_code == 422
    assert "billing.tax.product_class" in _actions(db_session)


# --- the tax engine switch (design 5) ---


def test_tax_switch_refused_then_accepted_and_rollback_lists_open_invoices(client, db_session, headers):
    from app.models.commerce_audit import UserAuditEvent as Event

    engines = "/api/admin/billing/engines"
    refused = client.post(f"{engines}/tax/switch", headers=headers, json={"to": "v2"})
    assert refused.status_code == 409, refused.text
    assert {"invoice_engine_v2", "tax_enabled_or_mirrored_only_confirmed"} <= set(refused.json()["detail"]["failing"])
    from datetime import datetime, timezone

    from tests.api.test_billing_p1c_engines_api import _seed_quiet_history

    _seed_quiet_history(db_session, "tax", 14, datetime.now(timezone.utc))
    set_engine(db_session, "invoice", "v2")
    db_session.commit()
    accepted = client.post(
        f"{engines}/tax/switch", headers=headers, json={"to": "v2", "confirm_mirrored_only": True}
    )
    assert accepted.status_code == 200, accepted.text
    back = client.post(f"{engines}/tax/switch", headers=headers, json={"to": "shadow"})
    assert back.status_code == 200 and back.json()["rollback"] == {"open_v2_taxed_invoices": 0}
    db_session.expire_all()
    events = db_session.scalars(select(Event).where(Event.action == "billing.engine.switch")).all()
    assert [e.resource_id for e in events][-2:] == ["tax", "tax"]
