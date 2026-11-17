"""Outgoing mail carries Date and Message-ID headers."""

from types import SimpleNamespace

from app.services import email_sender
from app.services.email_sender import message_id_domain


class _FakeSMTP:
    sent = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        pass

    def send_message(self, message):
        _FakeSMTP.sent.append(message)


def test_message_id_domain_handles_display_names():
    assert message_id_domain("noreply@example.com") == "example.com"
    assert message_id_domain("Rackflow <noreply@example.com>") == "example.com"
    assert message_id_domain("rackflow") is None


def test_deliver_sets_date_and_message_id(monkeypatch):
    _FakeSMTP.sent.clear()
    monkeypatch.setattr(email_sender.smtplib, "SMTP", _FakeSMTP)
    config = SimpleNamespace(
        smtp_from="Rackflow <noreply@example.com>",
        smtp_host="localhost",
        smtp_port=25,
        smtp_timeout_seconds=5,
        smtp_starttls=False,
        smtp_username=None,
        smtp_password=None,
    )

    email_sender.deliver(
        config, to_address="user@example.org", subject="Hi", body_text="Body", body_html=None
    )

    message = _FakeSMTP.sent[0]
    assert message["Date"]
    assert message["Message-ID"].endswith("@example.com>")
