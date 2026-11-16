"""Branded HTML email rendering."""

from app.services.email_layout import ACCENT, render_email_html
from app.services.email_message_service import EmailEvent, render_email


def test_password_reset_email_has_branded_html_with_button():
    url = "https://rackflow.test/reset-password?token=abc&x=1"
    subject, text, html = render_email(EmailEvent.PASSWORD_RESET, {"reset_url": url})
    assert subject == "Reset your Rackflow password"
    assert url in text
    assert html.startswith("<!DOCTYPE html>")
    assert ACCENT in html
    assert ">Reset password</a>" in html
    assert 'href="https://rackflow.test/reset-password?token=abc&amp;x=1"' in html


def test_every_event_gets_html():
    for event in [EmailEvent.WELCOME, EmailEvent.INVOICE_PAID, EmailEvent.TICKET_REPLY]:
        _, _, html = render_email(event, {})
        assert html and "Rackflow" in html


def test_layout_escapes_input():
    html = render_email_html(heading="<b>hi</b>", paragraphs=["<script>x</script>"])
    assert "<script>x" not in html and "&lt;script&gt;" in html
    assert "<b>hi</b>" not in html
