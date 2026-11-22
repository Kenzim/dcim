from pathlib import Path

import pytest

from app.plugin_host.testkit import assert_valid_package

PLUGIN = Path(__file__).resolve().parents[1]
URL = "https://discord.com/api/webhooks/1/abc"


@pytest.fixture
def plugin(rackflow):
    return rackflow.install(PLUGIN, settings={"webhook_url": URL})


def test_package_is_valid():
    assert_valid_package(PLUGIN)


def test_a_new_ticket_is_posted_to_discord(rackflow, plugin):
    rackflow.http.respond("discord.com", 204)
    result = rackflow.fire("ticket.created", ticket_number="TCK-9", subject="Server is down")
    assert len(result.delivered) == 1
    request = rackflow.http.requests[0]
    assert request.url == URL
    assert request.json()["content"] == "New ticket TCK-9: Server is down"
    assert request.json()["allowed_mentions"] == {"parse": []}


def test_slack_format(rackflow, plugin):
    plugin.configure(format="slack", webhook_url="https://hooks.slack.com/services/T/B/x")
    rackflow.fire("invoice.paid", number_display="INV-12", total_cents=1999, currency="GBP")
    assert rackflow.http.requests[0].json() == {"text": "Invoice INV-12 was paid: 19.99 GBP"}


def test_a_suspended_service_names_the_service(rackflow, plugin):
    service = rackflow.make_service(name="web-01")
    rackflow.set_status(service, "suspended")
    rackflow.deliver()
    assert rackflow.http.requests[0].json()["content"] == "Service web-01 (vm) was suspended"


def test_mentions_in_subjects_are_neutralised(rackflow, plugin):
    rackflow.fire("ticket.created", subject="@everyone <b>hi</b>")
    text = rackflow.http.requests[0].json()["content"]
    assert "@everyone" not in text and "<b>" not in text


def test_switched_off_messages_are_not_posted(rackflow, plugin):
    plugin.configure(notify_tickets=False)
    rackflow.fire("ticket.created")
    assert rackflow.http.requests == []


def test_the_secret_is_stored_but_never_shown(rackflow, plugin):
    assert plugin.secret("webhook_url") == URL
    assert URL not in str(plugin.settings())


def test_without_a_webhook_it_posts_nothing_and_says_why(rackflow):
    plugin = rackflow.install(PLUGIN)
    result = rackflow.fire("ticket.created")
    assert len(result.delivered) == 1
    assert rackflow.http.requests == []
    assert any("Webhook URL is not set" in m for m in plugin.log_messages("warning"))


def test_a_server_error_fails_the_delivery_so_it_is_retried(rackflow, plugin):
    rackflow.http.respond("discord.com", 503)
    result = rackflow.fire("ticket.created")
    assert [d.status for d in result.failed] == ["failed"]
    assert "503" in result.errors[0]
    assert plugin.installation.consecutive_failures == 1


def test_a_refused_request_is_logged_not_retried(rackflow, plugin):
    rackflow.http.respond("discord.com", 404)
    result = rackflow.fire("ticket.created")
    assert len(result.delivered) == 1
    assert any("status 404" in m for m in plugin.log_messages("error"))


def test_an_unreachable_chat_service_fails_the_delivery(rackflow, plugin):
    rackflow.http.fail("discord.com")
    assert rackflow.fire("ticket.created").failed


def test_a_repeated_event_is_posted_once(rackflow, plugin):
    from app.models.plugin_host import PluginDelivery

    rackflow.emit("ticket.created", ticket_number="TCK-1")
    row = rackflow.db.query(PluginDelivery).one()
    envelope = dict(row.envelope)
    rackflow.deliver()
    # The same event id arriving again, as after a crash between handler and acknowledgement.
    rackflow.db.add(
        PluginDelivery(
            event_id=envelope["id"] + "-again", plugin_id=plugin.id, event="ticket.created", envelope=dict(envelope),
            status="queued", attempts=0, next_attempt_at=row.created_at,
        )
    )
    rackflow.db.commit()
    rackflow.deliver()
    assert len(rackflow.http.requests) == 1
