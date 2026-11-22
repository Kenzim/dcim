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


def test_superseding_a_mirrored_rule_moves_the_legacy_link(client, db_session, headers):
    rate = TaxRate(name="UK", country="GB", region=None, rate_bps=2000, enabled=True)
    db_session.add(rate)
    db_session.flush()
    rule = TaxRuleMirror.sync(db_session, rate, now=datetime.now(timezone.utc) - timedelta(days=1))
    db_session.commit()
    resp = client.post(
        f"{BASE}/rules/{rule.id}/supersede", headers=headers, json={"rate_percent": "17.5", "valid_from": _later()}
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["legacy_tax_rate_id"] == rate.id
    db_session.expire_all()
    assert db_session.get(TaxRule, rule.id).legacy_tax_rate_id is None


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


def test_packs_and_id_checks_answer_501_until_built(client, headers):
    assert client.get(f"{BASE}/packs", headers=headers).status_code == 501


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
