"""Email templates: selection, rendering, enqueue, fallback and the senders (design 12)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.commerce_audit import UserAuditEvent
from app.models.commerce_email import EmailMessage, EmailMessageStatus, EmailTemplate, EmailTemplateVersion
from app.services.alerting import billing_integrity
from app.services.alerting.catalogue import CATALOGUE
from app.services.email_message_service import EmailEvent, EmailMessageService, render_email
from app.services.email_templates import catalogue
from app.services.email_templates.render import RENDER_FAILED_ACTION, render
from tests.migrations import helpers

REVISION = "billing_p2_email_templates"
ENABLED_TODAY = {
    "verify_email",
    "password_reset",
    "user_invite",
    "order_confirmation",
    "invoice_created",
    "invoice_paid",
    "service_suspended",
}
SENDERS_TODAY_SAMPLES = [
    {},
    {
        "invoice_number": "INV-2026-0001",
        "amount_cents": 1234,
        "currency": "gbp",
        "service_name": "vm-01",
        "order_number": "ORD-7",
        "verify_url": "https://example.com/v?a=1&b=2",
        "reset_url": "https://example.com/r",
        "invite_url": "https://example.com/i",
        "username": "bob",
        "ticket_number": "T-1",
    },
    {"invoice_id": 5, "order_id": 9, "ticket_id": 3, "amount_cents": 5, "currency": None, "service_name": None},
    {"invoice_number": 77, "amount_cents": -250, "currency": "EUR"},
]


def seed(db, *, only=None):
    module = helpers.load_revision(REVISION)
    for event, subject, body, enabled in module.SEED_TEMPLATES:
        if only and event not in only:
            continue
        row = EmailTemplate(event_key=event, locale="en", product_scope=0, subject=subject, body_text=body, enabled=enabled)
        db.add(row)
        db.flush()
        db.add(EmailTemplateVersion(template_id=row.id, version=1, subject=subject, body_text=body))
    db.commit()


def template(db, event, **fields):
    row = EmailTemplate(
        event_key=event,
        locale=fields.pop("locale", "en"),
        product_scope=fields.pop("product_scope", 0),
        subject=fields.pop("subject", "Subject"),
        body_text=fields.pop("body_text", "Body"),
        enabled=fields.pop("enabled", True),
        **fields,
    )
    db.add(row)
    db.commit()
    return row


# ------------------------------------------------------------------ catalogue and seeds


def test_catalogue_defaults_equal_the_seeded_templates():
    module = helpers.load_revision(REVISION)
    seeded = {event: (subject, body, enabled) for event, subject, body, enabled in module.SEED_TEMPLATES}
    assert set(seeded) == set(catalogue.EVENTS)
    for key, spec in catalogue.EVENTS.items():
        assert (spec.default_subject, spec.default_body) == seeded[key][:2], key
        assert spec.sends_today == seeded[key][2], key
    assert {key for key, spec in catalogue.EVENTS.items() if spec.sends_today} == ENABLED_TODAY
    assert [key for key, spec in catalogue.EVENTS.items() if spec.accepts_product] == ["welcome"]


def test_every_event_has_documented_samples_that_cover_its_template_variables():
    for spec in catalogue.EVENTS.values():
        assert catalogue.builtin_email(spec.key, spec.samples)[1]
        for variable in spec.variables:
            assert variable.name and variable.type and variable.description


@pytest.mark.parametrize("sample", SENDERS_TODAY_SAMPLES)
def test_seeded_templates_render_exactly_todays_mail(db_session, sample):
    """Through ``render``, seeded text, subject and HTML equal the built-in output for every event that sends today."""
    seed(db_session)
    for event in catalogue.EVENTS:
        expected_subject, expected_text, expected_html = render_email(event, dict(sample))
        known = {name for name in catalogue.EVENTS[event].samples}
        rendered = render(db_session, event, locale="en", variables={k: v for k, v in sample.items() if k in known})
        assert not rendered.fallback, event
        if event in {"invoice_reminder", "late_fee_added", "service_termination_warning", "cancellation_requested"}:
            continue  # their texts need variables no sample here carries; covered below
        assert rendered.subject == expected_subject, event
        assert rendered.text == expected_text, event
        assert rendered.html == expected_html, event
        assert rendered.skipped is (event not in ENABLED_TODAY), event


def test_new_event_texts_equal_the_built_in_text_with_samples():
    for key in ("invoice_reminder", "late_fee_added", "service_termination_warning", "cancellation_requested",
                "service_terminated", "cancellation_completed", "tax_id_unverified"):
        spec = catalogue.EVENTS[key]
        subject, text, _html = catalogue.builtin_email(key, spec.samples)
        assert subject == spec.default_subject
        from app.services.email_templates.sandbox import render_source

        assert render_source(spec.default_body, spec.samples) == text, key


# ------------------------------------------------------------------ selection


def test_template_choice_is_product_then_locale_then_english_then_builtin(db_session):
    english = template(db_session, "welcome", subject="en", body_text="en body")
    german = template(db_session, "welcome", locale="de", subject="de", body_text="de body")
    product = template(db_session, "welcome", product_scope=9, subject="p9", body_text="p9 body")

    def pick(**kwargs):
        return render(db_session, "welcome", variables={"username": "u"}, **kwargs)

    assert pick(locale="en", product_id=9).template_id == product.id
    assert pick(locale="en", product_id=10).template_id == english.id
    assert pick(locale="de", product_id=9).template_id == german.id  # the product variant is for English only
    assert pick(locale="de").template_id == german.id
    assert pick(locale="fr").template_id == english.id
    db_session.delete(english)
    db_session.commit()
    built_in = pick(locale="fr")
    assert (built_in.template_id, built_in.fallback, built_in.skipped) == (None, False, False)
    assert built_in.text == "Welcome to Rackflow, u! Your account is ready."


def test_unknown_event_uses_the_generic_built_in_text(db_session):
    rendered = render(db_session, "no_such_event", locale="en", variables={})
    assert (rendered.subject, rendered.text, rendered.template_id) == (
        "Rackflow notification",
        "There is an update to your Rackflow account.",
        None,
    )


def test_render_stores_template_id_and_version(db_session):
    row = template(db_session, "invoice_paid", subject="Paid {{ invoice_number }}", body_text="{{ amount_cents|money(currency) }}", version=4)
    rendered = render(
        db_session, "invoice_paid", locale="en", variables={"invoice_number": "I-1", "amount_cents": 500, "currency": "GBP"}
    )
    assert (rendered.subject, rendered.text, rendered.template_id, rendered.template_version) == ("Paid I-1", "GBP 5.00", row.id, 4)
    assert "Paid I-1" in rendered.html


def test_html_template_is_rendered_with_autoescape(db_session):
    template(db_session, "welcome", body_html="<p>Hi {{ username }}</p>")
    rendered = render(db_session, "welcome", locale="en", variables={"username": "<script>x</script>"})
    assert rendered.html == "<p>Hi &lt;script&gt;x&lt;/script&gt;</p>"
    assert rendered.text == "Body"


def test_subject_is_one_line(db_session):
    template(db_session, "welcome", subject="Hi\r\nBcc: x@example.com {{ username }}")
    rendered = render(db_session, "welcome", locale="en", variables={"username": "a\nb"})
    assert "\n" not in rendered.subject and "\r" not in rendered.subject


def test_variables_outside_the_catalogue_are_not_visible_to_the_template(db_session):
    template(db_session, "welcome", body_text="{{ username }}{{ secret }}")
    rendered = render(db_session, "welcome", locale="en", variables={"username": "u", "secret": "s"})
    assert rendered.fallback is True


# ------------------------------------------------------------------ failure, disabled


def test_failing_template_falls_back_audits_and_marks_the_message(db_session):
    row = template(db_session, "invoice_created", body_text="{{ not_a_variable }}", version=3)
    message = EmailMessageService.enqueue(
        db_session,
        to_address="a@example.com",
        event=EmailEvent.INVOICE_CREATED,
        idempotency_key="invoice:1:created",
        variables={"invoice_number": 12, "amount_cents": 1000, "currency": "GBP"},
    )
    assert message.status == EmailMessageStatus.QUEUED
    assert message.body_text == "Invoice 12 for GBP 10.00 is now available in your account."
    assert message.subject == "New Rackflow invoice"
    assert (message.render_fallback, message.template_id, message.template_version) == (True, row.id, 3)
    audit = db_session.execute(select(UserAuditEvent).where(UserAuditEvent.action == RENDER_FAILED_ACTION)).scalar_one()
    assert audit.resource_id == str(row.id)
    assert "not_a_variable" in audit.event_metadata["error"]
    assert audit.event_metadata["event_key"] == "invoice_created"


def test_money_email_survives_a_security_error_in_its_template(db_session):
    template(db_session, "invoice_paid", body_text="{{ invoice_number.__class__ }}")
    message = EmailMessageService.enqueue(
        db_session, to_address="a@example.com", event="invoice_paid", idempotency_key="k1",
        variables={"invoice_number": 1, "amount_cents": 100, "currency": "USD"},
    )
    assert message.status == EmailMessageStatus.QUEUED
    assert message.render_fallback is True
    assert message.body_text == "Payment of USD 1.00 for invoice 1 was received. Thank you."


def test_disabled_template_stores_a_skipped_message(db_session):
    row = template(db_session, "ticket_reply", enabled=False)
    message = EmailMessageService.enqueue(
        db_session, to_address="a@example.com", event="ticket_reply", idempotency_key="k2",
        variables={"ticket_number": "T-1"},
    )
    assert message.status == EmailMessageStatus.SKIPPED
    assert message.template_id == row.id
    assert message.render_fallback is False
    assert message.body_text == "There is a new reply on support ticket T-1."


def test_rendered_text_is_stored_at_enqueue_and_later_edits_do_not_change_it(db_session):
    row = template(db_session, "service_provisioned", subject="Ready", body_text="{{ service_name }} is up")
    message = EmailMessageService.enqueue(
        db_session, to_address="a@example.com", event="service_provisioned", idempotency_key="k3",
        variables={"service_name": "vm-1"},
    )
    assert message.body_text == "vm-1 is up"
    row.body_text = "changed"
    db_session.commit()
    again = EmailMessageService.enqueue(
        db_session, to_address="a@example.com", event="service_provisioned", idempotency_key="k3",
        variables={"service_name": "vm-1"},
    )
    assert again.id == message.id and again.body_text == "vm-1 is up"
    db_session.refresh(message)
    assert message.body_text == "vm-1 is up"


def test_enqueue_without_variables_keeps_the_built_in_path(db_session):
    template(db_session, "invoice_paid", body_text="from template")
    message = EmailMessageService.enqueue(
        db_session, to_address="a@example.com", event="invoice_paid", idempotency_key="k4",
        data={"invoice_number": 3, "amount_cents": 100, "currency": "USD"},
    )
    assert message.body_text == "Payment of USD 1.00 for invoice 3 was received. Thank you."
    assert message.template_id is None


def test_enqueue_still_validates_the_recipient(db_session):
    with pytest.raises(ValueError):
        EmailMessageService.enqueue(db_session, to_address="nope", event="welcome", idempotency_key="k5", variables={})


# ------------------------------------------------------------------ detector


def test_detector_reports_templates_that_fell_back(db_session):
    now = datetime.now(timezone.utc)
    assert billing_integrity.email_template_render_failed(db_session, {"max_rows": 25}, now) == []
    row = template(db_session, "invoice_paid", body_text="{{ nope }}")
    for number in range(3):
        EmailMessageService.enqueue(
            db_session, to_address="a@example.com", event="invoice_paid", idempotency_key=f"d{number}",
            variables={"invoice_number": number},
        )
    EmailMessageService.enqueue(
        db_session, to_address="a@example.com", event="invoice_created", idempotency_key="healthy",
        variables={"invoice_number": 1},
    )
    findings = billing_integrity.email_template_render_failed(db_session, {"max_rows": 25}, now)
    assert [(f.alert_type, f.resource_type, f.resource_id) for f in findings] == [
        ("billing_email_template_render_failed", "email_template", str(row.id))
    ]
    assert findings[0].details["messages"] == 3
    assert findings[0].details["event"] == "invoice_paid"
    later = now + timedelta(days=billing_integrity.EMAIL_RENDER_FAILED_LOOKBACK_DAYS + 1)
    assert billing_integrity.email_template_render_failed(db_session, {"max_rows": 25}, later) == []
    assert CATALOGUE["billing_email_template_render_failed"].enabled_by_default is False


def test_detector_limits_its_rows(db_session):
    now = datetime.now(timezone.utc)
    for index in range(4):
        template(db_session, ["invoice_paid", "invoice_created", "ticket_reply", "welcome"][index], body_text="{{ nope }}")
    for index, event in enumerate(["invoice_paid", "invoice_created", "ticket_reply", "welcome"]):
        EmailMessageService.enqueue(
            db_session, to_address="a@example.com", event=event, idempotency_key=f"l{index}", variables={}
        )
    findings = billing_integrity.email_template_render_failed(db_session, {"max_rows": 2}, now)
    assert len(findings) == 3 and findings[-1].resource_id == billing_integrity.OVERFLOW_ID
    assert findings[-1].details["remaining"] == 2


# ------------------------------------------------------------------ senders


def _user(db, name="alex", email=None):
    from app.models.user import User

    user = User(username=name, email=email or f"{name}@example.com", is_admin=False)
    user.set_password("password123")
    db.add(user)
    db.flush()
    return user


def test_send_service_provisioned_queues_one_message(db_session):
    from app.models.service import Service
    from app.services.billing import email_events

    owner = _user(db_session)
    service = Service(name="vm-9", owner_user_id=owner.id)
    db_session.add(service)
    db_session.commit()
    seed(db_session, only={"service_provisioned"})
    skipped = email_events.send_service_provisioned(db_session, service)
    assert skipped.status == EmailMessageStatus.SKIPPED  # seeded disabled: nothing new at deploy
    row = db_session.execute(select(EmailTemplate).where(EmailTemplate.event_key == "service_provisioned")).scalar_one()
    row.enabled = True
    db_session.commit()
    again = email_events.send_service_provisioned(db_session, service)
    assert again.id == skipped.id  # one message per service, whatever the template says later
    db_session.delete(skipped)
    db_session.commit()
    sent = email_events.send_service_provisioned(db_session, service)
    assert (sent.status, sent.to_address, sent.body_text, sent.idempotency_key) == (
        EmailMessageStatus.QUEUED, "alex@example.com", "vm-9 has been provisioned and is ready to use.",
        f"service:{service.id}:provisioned",
    )
    assert sent.user_id == owner.id


def test_send_service_provisioned_without_owner_email_does_nothing(db_session):
    from app.models.service import Service
    from app.services.billing import email_events

    service = Service(name="orphan")
    db_session.add(service)
    db_session.commit()
    assert email_events.send_service_provisioned(db_session, service) is None


def test_send_welcome_uses_the_variant_of_the_first_orders_product(db_session):
    from app.services.billing import email_events

    user = _user(db_session, "newbie")
    template(db_session, "welcome", subject="Generic", body_text="generic {{ username }}")
    template(db_session, "welcome", product_scope=4, subject="Proxy", body_text="proxy {{ username }}")
    db_session.commit()
    message = email_events.send_welcome(db_session, user)
    assert (message.subject, message.body_text, message.idempotency_key) == ("Generic", "generic newbie", f"user:{user.id}:welcome")


def test_send_welcome_picks_the_product_of_the_first_order(db_session):
    from app.models.commerce_account import BillingAccount
    from app.models.commerce_order import Order, OrderItem
    from app.models.product_catalog import Product
    from app.models.storefront import FrontendProduct, PricePlan, PricePlanPricingModel
    from app.services.billing import email_events

    user = _user(db_session, "buyer")
    product = Product(name="Proxy", code="proxy")
    db_session.add(product)
    db_session.flush()
    frontend = FrontendProduct(name="Proxy", slug="proxy", product_id=product.id, service_type="vm")
    db_session.add(frontend)
    db_session.flush()
    plan = PricePlan(frontend_product_id=frontend.id, name="m", currency="USD", pricing_model=PricePlanPricingModel.FREE)
    db_session.add(plan)
    account = db_session.execute(select(BillingAccount).where(BillingAccount.user_id == user.id)).scalar_one_or_none()
    if account is None:
        from app.dao.commerce_dao import BillingAccountDAO

        account = BillingAccountDAO.ensure_client_account(db_session, user.id)
    db_session.flush()
    order = Order(order_number=1001, billing_account_id=account.id, user_id=user.id, currency="USD")
    db_session.add(order)
    db_session.flush()
    db_session.add(OrderItem(order_id=order.id, frontend_product_id=frontend.id, price_plan_id=plan.id, name_snapshot="Proxy"))
    template(db_session, "welcome", subject="Generic", body_text="generic")
    variant = template(db_session, "welcome", product_scope=product.id, subject="Proxy", body_text="proxy {{ username }}")
    message = email_events.send_welcome(db_session, user)
    assert message.template_id == variant.id
    assert message.body_text == "proxy buyer"
