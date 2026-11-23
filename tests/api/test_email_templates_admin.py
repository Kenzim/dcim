"""Admin API for the email templates (design 12.4)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.commerce_audit import UserAuditEvent
from app.models.commerce_email import EmailMessage, EmailMessageStatus, EmailTemplate, EmailTemplateVersion
from app.models.user import User
from app.services.email_templates import admin as templates_admin
from app.services.email_templates import catalogue
from tests.services.test_email_templates import seed

BASE = "/api/admin/commerce/email-templates"


@pytest.fixture
def headers(client, test_admin_user):
    response = client.post("/api/users/login", json={"username": "admin", "password": "adminpassword123"})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


@pytest.fixture
def seeded(db_session):
    seed(db_session)
    return db_session


def _id(db, event):
    return db.execute(select(EmailTemplate.id).where(EmailTemplate.event_key == event, EmailTemplate.product_scope == 0)).scalar_one()


def _actions(db, prefix="billing.email_template."):
    return [row.action for row in db.execute(select(UserAuditEvent).order_by(UserAuditEvent.id)).scalars() if row.action.startswith(prefix)]


def test_every_route_needs_an_admin(client, seeded):
    assert client.get(BASE).status_code in {401, 403}
    user = User(username="plain", email="plain@example.com", is_admin=False)
    user.set_password("password123")
    seeded.add(user)
    seeded.commit()
    login = client.post("/api/users/login", json={"username": "plain", "password": "password123"})
    plain = {"Authorization": f"Bearer {login.json()['token']}"}
    for method, path in [("get", BASE), ("get", f"{BASE}/events"), ("put", f"{BASE}/1"), ("post", f"{BASE}/1/test-send"),
                         ("post", f"{BASE}/1/reset"), ("post", BASE)]:
        assert getattr(client, method)(path, headers=plain).status_code == 403, (method, path)


def test_list_and_events(client, seeded, headers):
    listed = client.get(BASE, headers=headers).json()["templates"]
    assert {row["event_key"] for row in listed} == set(catalogue.EVENTS)
    assert all("body_text" not in row for row in listed)
    welcome = client.get(f"{BASE}?event_key=welcome", headers=headers).json()["templates"]
    assert [row["event_key"] for row in welcome] == ["welcome"]
    events = client.get(f"{BASE}/events", headers=headers).json()
    keys = [event["key"] for event in events["events"]]
    assert keys == list(catalogue.EVENTS)
    reminder = next(event for event in events["events"] if event["key"] == "invoice_reminder")
    assert {variable["name"] for variable in reminder["variables"]} >= {"invoice_number", "due_date", "amount_cents"}
    assert reminder["variables"][0]["sample"] is not None
    assert "money" in events["filters"] and "format" not in events["filters"]
    assert events["limits"]["source_bytes"] == 64 * 1024


def test_get_returns_the_body_and_event_documentation(client, seeded, headers):
    row = client.get(f"{BASE}/{_id(seeded, 'invoice_paid')}", headers=headers).json()
    assert row["body_text"].startswith("Payment of ")
    assert row["body_html"] is None
    assert row["event"]["variables"]
    assert client.get(f"{BASE}/99999", headers=headers).status_code == 404


def test_put_saves_a_new_version_and_audits(client, seeded, headers):
    template_id = _id(seeded, "invoice_paid")
    response = client.put(
        f"{BASE}/{template_id}", headers=headers,
        json={"subject": "Paid {{ invoice_number }}", "body_text": "Thanks for {{ amount_cents|money(currency) }}.", "enabled": False},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["version"], body["enabled"], body["subject"]) == (2, False, "Paid {{ invoice_number }}")
    versions = seeded.execute(select(EmailTemplateVersion).where(EmailTemplateVersion.template_id == template_id)).scalars().all()
    assert sorted(v.version for v in versions) == [1, 2]
    assert "billing.email_template.update" in _actions(seeded)
    listed = client.get(f"{BASE}/{template_id}/versions", headers=headers).json()["versions"]
    assert [v["version"] for v in listed] == [2, 1]


@pytest.mark.parametrize(
    ("field", "value", "kind", "needle"),
    [
        ("body_text", "Hello {{ nope }}", "undefined", "nope"),
        ("body_text", "line\n{{ name.__class__ }}", "security", "__class__"),
        ("body_text", "{% include 'x' %}", "security", "include"),
        ("body_text", "{{ invoice_number", "syntax", ""),
        ("body_text", "x" * (64 * 1024 + 1), "limit", "64 KB"),
        ("subject", "{{ nope }}", "undefined", "nope"),
        ("body_html", "<p>{{ nope }}</p>", "undefined", "nope"),
        ("body_html", "{{ invoice_number.__class__.__mro__ }}", "security", "not allowed"),
    ],
)
def test_put_validates_by_rendering_against_samples(client, seeded, headers, field, value, kind, needle):
    template_id = _id(seeded, "invoice_paid")
    payload = {"subject": "Paid", "body_text": "ok {{ invoice_number }}", "body_html": None}
    payload[field] = value
    response = client.put(f"{BASE}/{template_id}", headers=headers, json=payload)
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "template_invalid"
    (error,) = [e for e in detail["errors"] if e["field"] == field]
    assert error["kind"] == kind
    assert needle in error["message"]
    row = seeded.get(EmailTemplate, template_id)
    seeded.refresh(row)
    assert row.version == 1


def test_put_reports_the_line_of_an_error(client, seeded, headers):
    template_id = _id(seeded, "invoice_paid")
    response = client.put(
        f"{BASE}/{template_id}", headers=headers, json={"subject": "s", "body_text": "one\ntwo\n{{ missing }}"}
    )
    (error,) = response.json()["detail"]["errors"]
    assert (error["field"], error["line"]) == ("body_text", 3)


def test_put_collects_every_field_error(client, seeded, headers):
    template_id = _id(seeded, "invoice_paid")
    response = client.put(
        f"{BASE}/{template_id}", headers=headers, json={"subject": "{{ a }}", "body_text": "{{ b }}", "body_html": "{{ c }}"}
    )
    assert sorted(e["field"] for e in response.json()["detail"]["errors"]) == ["body_html", "body_text", "subject"]


def test_put_refuses_empty_texts_and_unknown_fields(client, seeded, headers):
    template_id = _id(seeded, "invoice_paid")
    assert client.put(f"{BASE}/{template_id}", headers=headers, json={"subject": " ", "body_text": "x"}).status_code == 422
    assert client.put(f"{BASE}/{template_id}", headers=headers, json={"subject": "s", "body_text": " "}).status_code == 422
    assert client.put(f"{BASE}/{template_id}", headers=headers, json={"subject": "s", "body_text": "x", "event_key": "y"}).status_code == 422
    assert client.put(f"{BASE}/99999", headers=headers, json={"subject": "s", "body_text": "x"}).status_code == 404


def _versions(db, template_id):
    return sorted(
        db.execute(select(EmailTemplateVersion.version).where(EmailTemplateVersion.template_id == template_id)).scalars()
    )


def test_enabling_a_template_with_unchanged_text_keeps_its_version(client, seeded, headers):
    template_id = _id(seeded, "ticket_reply")
    row = client.get(f"{BASE}/{template_id}", headers=headers).json()
    response = client.put(
        f"{BASE}/{template_id}", headers=headers,
        json={"subject": row["subject"], "body_text": row["body_text"], "body_html": row["body_html"], "enabled": True},
    )
    assert response.status_code == 200, response.text
    assert response.json()["enabled"] is True and response.json()["version"] == row["version"] == 1
    assert _versions(seeded, template_id) == [1]
    metadata = seeded.execute(select(UserAuditEvent).where(UserAuditEvent.action == "billing.email_template.update")).scalar_one().event_metadata
    assert (metadata["enabled_before"], metadata["enabled_after"], metadata["version"]) == (False, True, 1)


def test_a_toggle_needs_only_the_enabled_flag(client, seeded, headers):
    template_id = _id(seeded, "ticket_reply")
    before = client.get(f"{BASE}/{template_id}", headers=headers).json()
    on = client.put(f"{BASE}/{template_id}", headers=headers, json={"enabled": True})
    assert on.status_code == 200, on.text
    assert on.json()["enabled"] is True and on.json()["version"] == 1
    assert (on.json()["subject"], on.json()["body_text"]) == (before["subject"], before["body_text"])
    off = client.put(f"{BASE}/{template_id}", headers=headers, json={"enabled": False})
    assert off.json()["enabled"] is False and off.json()["version"] == 1
    assert _versions(seeded, template_id) == [1]
    assert _actions(seeded).count("billing.email_template.update") == 2


def test_changing_the_text_still_saves_a_version_whether_or_not_enabled_changes(client, seeded, headers):
    template_id = _id(seeded, "ticket_reply")
    row = client.get(f"{BASE}/{template_id}", headers=headers).json()
    edited = client.put(
        f"{BASE}/{template_id}", headers=headers,
        json={"subject": row["subject"] + " (edited)", "body_text": row["body_text"], "enabled": True},
    )
    assert edited.json()["version"] == 2 and edited.json()["enabled"] is True
    html = client.put(
        f"{BASE}/{template_id}", headers=headers,
        json={"subject": edited.json()["subject"], "body_text": row["body_text"], "body_html": "<p>Hi</p>"},
    )
    assert html.json()["version"] == 3
    assert _versions(seeded, template_id) == [1, 2, 3]


def test_a_put_with_only_one_of_subject_and_body_text_is_refused(client, seeded, headers):
    template_id = _id(seeded, "ticket_reply")
    assert client.put(f"{BASE}/{template_id}", headers=headers, json={"subject": "s"}).status_code == 422
    assert client.put(f"{BASE}/{template_id}", headers=headers, json={"body_text": "x"}).status_code == 422
    assert client.put(f"{BASE}/{template_id}", headers=headers, json={"body_html": "<p>x</p>"}).status_code == 422
    assert client.put(f"{BASE}/{template_id}", headers=headers, json={}).status_code == 422


def test_preview_never_sends_and_takes_overrides_and_drafts(client, seeded, headers):
    template_id = _id(seeded, "invoice_paid")
    before = seeded.execute(select(EmailMessage)).scalars().all()
    plain = client.post(f"{BASE}/{template_id}/preview", headers=headers).json()
    assert plain["text"] == "Payment of GBP 12.34 for invoice INV-2026-0001 was received. Thank you."
    assert plain["html"].startswith("<!DOCTYPE html>")
    over = client.post(
        f"{BASE}/{template_id}/preview", headers=headers, json={"variables": {"invoice_number": "X-9", "amount_cents": 5}}
    ).json()
    assert over["text"] == "Payment of GBP 0.05 for invoice X-9 was received. Thank you."
    draft = client.post(
        f"{BASE}/{template_id}/preview", headers=headers,
        json={"subject": "S {{ invoice_number }}", "body_text": "draft", "body_html": "<b>{{ invoice_number }}</b>"},
    ).json()
    assert draft == {"subject": "S INV-2026-0001", "text": "draft", "html": "<b>INV-2026-0001</b>"}
    bad = client.post(f"{BASE}/{template_id}/preview", headers=headers, json={"body_text": "{{ nope }}"})
    assert bad.status_code == 422
    unknown = client.post(f"{BASE}/{template_id}/preview", headers=headers, json={"variables": {"zzz": 1}})
    assert unknown.status_code == 422
    assert seeded.execute(select(EmailMessage)).scalars().all() == before
    assert client.get(f"{BASE}/{template_id}", headers=headers).json()["version"] == 1


def test_test_send_goes_to_the_calling_admin_and_is_marked(client, seeded, headers, test_admin_user):
    template_id = _id(seeded, "ticket_reply")  # disabled, yet a test may be sent
    response = client.post(f"{BASE}/{template_id}/test-send", headers=headers)
    assert response.status_code == 201, response.text
    message = seeded.get(EmailMessage, response.json()["message_id"])
    assert message.to_address == "admin@example.com"
    assert message.subject.startswith("[Test] ")
    assert (message.status, message.template_id, message.related_type) == (EmailMessageStatus.QUEUED, template_id, "email_template_test")
    assert message.event == "ticket_reply" and message.template_key == "test:ticket_reply"
    assert "billing.email_template.test_send" in _actions(seeded)


def test_test_send_to_a_given_address_and_validation(client, seeded, headers):
    template_id = _id(seeded, "ticket_reply")
    ok = client.post(f"{BASE}/{template_id}/test-send", headers=headers, json={"to_address": "someone@example.org"})
    assert ok.status_code == 201 and ok.json()["to_address"] == "someone@example.org"
    for bad in ("nope", "a@b.c\r\nBcc: x@example.com", "a@example.com, b@example.com", "<a@example.com>"):
        response = client.post(f"{BASE}/{template_id}/test-send", headers=headers, json={"to_address": bad})
        assert response.status_code == 422, bad


def test_test_send_is_limited_to_ten_per_admin_per_hour(client, seeded, headers, test_admin_user):
    template_id = _id(seeded, "ticket_reply")
    for _ in range(templates_admin.TEST_SEND_LIMIT):
        assert client.post(f"{BASE}/{template_id}/test-send", headers=headers).status_code == 201
    limited = client.post(f"{BASE}/{template_id}/test-send", headers=headers)
    assert limited.status_code == 429
    assert 0 < int(limited.headers["Retry-After"]) <= 3600
    count = len(seeded.execute(select(EmailMessage).where(EmailMessage.related_type == "email_template_test")).scalars().all())
    assert count == templates_admin.TEST_SEND_LIMIT
    # Other admins have their own allowance, and old sends age out.
    other = User(username="admin2", email="admin2@example.com", is_admin=True)
    other.set_password("adminpassword123")
    seeded.add(other)
    seeded.commit()
    login = client.post("/api/users/login", json={"username": "admin2", "password": "adminpassword123"})
    other_headers = {"Authorization": f"Bearer {login.json()['token']}"}
    assert client.post(f"{BASE}/{template_id}/test-send", headers=other_headers).status_code == 201
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    for message in seeded.execute(select(EmailMessage).where(EmailMessage.idempotency_key.startswith(f"email_template_test:{test_admin_user.id}:"))).scalars():
        message.created_at = old
    seeded.commit()
    assert client.post(f"{BASE}/{template_id}/test-send", headers=headers).status_code == 201


def test_a_limited_test_send_still_validates_nothing_it_would_send(client, seeded, headers):
    template_id = _id(seeded, "invoice_paid")
    seeded.execute(EmailTemplate.__table__.update().where(EmailTemplate.id == template_id).values(body_text="{{ nope }}"))
    seeded.commit()
    assert client.post(f"{BASE}/{template_id}/test-send", headers=headers).status_code == 422
    assert seeded.execute(select(EmailMessage)).scalars().all() == []


def test_reset_saves_the_built_in_text_as_a_new_version(client, seeded, headers):
    template_id = _id(seeded, "invoice_paid")
    client.put(f"{BASE}/{template_id}", headers=headers, json={"subject": "Custom", "body_text": "Custom {{ invoice_number }}", "body_html": "<p>x</p>"})
    response = client.post(f"{BASE}/{template_id}/reset", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    spec = catalogue.EVENTS["invoice_paid"]
    assert (body["version"], body["subject"], body["body_text"], body["body_html"]) == (3, spec.default_subject, spec.default_body, None)
    assert "billing.email_template.reset" in _actions(seeded)
    assert body["enabled"] is True


def test_restore_saves_an_old_version_as_a_new_one(client, seeded, headers):
    template_id = _id(seeded, "invoice_paid")
    client.put(f"{BASE}/{template_id}", headers=headers, json={"subject": "Second", "body_text": "second"})
    restored = client.post(f"{BASE}/{template_id}/restore/1", headers=headers)
    assert restored.status_code == 200, restored.text
    assert (restored.json()["version"], restored.json()["subject"]) == (3, catalogue.EVENTS["invoice_paid"].default_subject)
    assert client.post(f"{BASE}/{template_id}/restore/9", headers=headers).status_code == 404
    assert "billing.email_template.restore" in _actions(seeded)


def test_create_a_locale_and_a_product_variant(client, seeded, headers):
    from app.models.product_catalog import Product

    product = Product(name="Proxy", code="proxy")
    seeded.add(product)
    seeded.commit()
    german = client.post(
        BASE, headers=headers, json={"event_key": "invoice_paid", "locale": "de", "subject": "Bezahlt", "body_text": "Danke {{ invoice_number }}", "enabled": True},
    )
    assert german.status_code == 201, german.text
    assert (german.json()["locale"], german.json()["version"], german.json()["enabled"]) == ("de", 1, True)
    assert client.post(BASE, headers=headers, json={"event_key": "invoice_paid", "locale": "de", "subject": "x", "body_text": "x"}).status_code == 409
    welcome = client.post(
        BASE, headers=headers, json={"event_key": "welcome", "product_scope": product.id, "subject": "Hi", "body_text": "Hi {{ username }}"},
    )
    assert welcome.status_code == 201 and welcome.json()["enabled"] is False
    assert client.post(BASE, headers=headers, json={"event_key": "invoice_paid", "product_scope": product.id, "subject": "x", "body_text": "x"}).status_code == 422
    assert client.post(BASE, headers=headers, json={"event_key": "welcome", "product_scope": 9999, "subject": "x", "body_text": "x"}).status_code == 422
    assert client.post(BASE, headers=headers, json={"event_key": "nope", "subject": "x", "body_text": "x"}).status_code == 422
    assert client.post(BASE, headers=headers, json={"event_key": "welcome", "locale": "../x", "subject": "x", "body_text": "x"}).status_code == 422
    assert client.post(BASE, headers=headers, json={"event_key": "welcome", "locale": "fr", "subject": "x", "body_text": "{{ bad"}).status_code == 422
    assert _actions(seeded).count("billing.email_template.create") == 2


def test_test_send_locks_the_admin_before_it_counts(seeded, test_admin_user, monkeypatch):
    """Counting then inserting without a lock let 5 threads with 9 earlier sends make 14 in the hour.

    SQLite ignores FOR UPDATE, so this asserts the statements: the admin's user
    row is locked first, and the count itself is a locking read (MariaDB
    repeatable read would otherwise count from an older snapshot).
    """
    from sqlalchemy.dialects import mysql

    row = seeded.get(EmailTemplate, _id(seeded, "ticket_reply"))
    seen = []
    real_execute = seeded.execute

    def recording_execute(statement, *args, **kwargs):
        seen.append(statement)
        return real_execute(statement, *args, **kwargs)

    monkeypatch.setattr(seeded, "execute", recording_execute)
    templates_admin.queue_test_send(seeded, row, admin_user_id=test_admin_user.id, to_address="a@example.com")
    locking = [statement for statement in seen if getattr(statement, "_for_update_arg", None) is not None]
    assert len(locking) == 2
    first, second = (str(statement.compile(dialect=mysql.dialect())) for statement in locking)
    assert "FROM users" in first and first.rstrip().endswith("FOR UPDATE")
    assert "FROM email_messages" in second and second.rstrip().endswith("FOR UPDATE")
    assert seen.index(locking[0]) < seen.index(locking[1])


def test_every_test_send_is_audited_with_its_address(client, seeded, headers):
    template_id = _id(seeded, "ticket_reply")
    for address in ("one@example.org", "two@example.net"):
        assert client.post(f"{BASE}/{template_id}/test-send", headers=headers, json={"to_address": address}).status_code == 201
    audits = seeded.execute(select(UserAuditEvent).where(UserAuditEvent.action == "billing.email_template.test_send").order_by(UserAuditEvent.id)).scalars().all()
    assert [audit.event_metadata["to_address"] for audit in audits] == ["one@example.org", "two@example.net"]
    assert all(audit.event_metadata["message_id"] for audit in audits)


def test_preview_is_limited_per_admin_per_minute(client, seeded, headers, mock_redis, monkeypatch):
    import app.core.rate_limit as rate_limit_module
    from app.api import email_templates_admin as api

    monkeypatch.setattr(rate_limit_module, "redis_client", mock_redis)
    template_id = _id(seeded, "invoice_paid")
    assert api.PREVIEW_LIMIT == 60 and api.PREVIEW_WINDOW_SECONDS == 60
    for _ in range(api.PREVIEW_LIMIT):
        assert client.post(f"{BASE}/{template_id}/preview", headers=headers).status_code == 200
    limited = client.post(f"{BASE}/{template_id}/preview", headers=headers)
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) > 0
    # Saving, listing and test sending are not affected by the preview counter.
    assert client.get(BASE, headers=headers).status_code == 200


def test_retry_route_answers_409_for_mail_skipped_by_a_disabled_template(client, seeded, headers):
    from app.services.email_message_service import EmailMessageService

    message = EmailMessageService.enqueue(
        seeded, to_address="a@example.com", event="ticket_reply", idempotency_key="route-retry",
        variables={"ticket_number": "T-1"},
    )
    seeded.commit()
    assert message.status == EmailMessageStatus.SKIPPED
    refused = client.post(f"/api/admin/commerce/email-messages/{message.id}/retry", headers=headers)
    assert refused.status_code == 409
    assert "ticket_reply" in refused.json()["detail"] and "disabled" in refused.json()["detail"]
    seeded.execute(EmailTemplate.__table__.update().where(EmailTemplate.event_key == "ticket_reply").values(enabled=True))
    seeded.commit()
    again = client.post(f"/api/admin/commerce/email-messages/{message.id}/retry", headers=headers)
    assert again.status_code == 200 and again.json()["status"] == "queued"
