"""Markdown sanitizer tests."""

from app.services.markdown_sanitize import sanitize_markdown_to_html


def test_sanitize_bold_and_link():
    html = sanitize_markdown_to_html("**hi** [x](https://example.com)")
    assert "<strong>hi</strong>" in html
    assert 'href="https://example.com"' in html


def test_sanitize_truncates_huge_input():
    html = sanitize_markdown_to_html("x" * 100_000)
    assert len(html) < 200_000
