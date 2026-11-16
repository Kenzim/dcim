"""Branded HTML layout for outbound account emails.

Email clients ignore stylesheets and CSS variables, so the site's light-theme
colours are inlined on table-based markup.
"""

from __future__ import annotations

from html import escape
from typing import Optional

ACCENT = "#0e7490"
ACCENT_CONTRAST = "#ffffff"
PAGE_BG = "#f1f4f8"
CARD_BG = "#ffffff"
BORDER = "#d7dee8"
TEXT = "#0f172a"
TEXT_MUTED = "#475569"
FONT = "'IBM Plex Sans', 'Segoe UI', 'Helvetica Neue', Arial, sans-serif"


def render_email_html(
    *,
    heading: str,
    paragraphs: list[str],
    action_url: Optional[str] = None,
    action_label: Optional[str] = None,
    footnote: Optional[str] = None,
) -> str:
    """Render plain-text paragraphs into the branded layout (all input is escaped)."""
    body = "".join(
        f'<p style="margin:0 0 16px;font-size:15px;line-height:1.6;color:{TEXT};">'
        f"{escape(p).replace(chr(10), '<br>')}</p>"
        for p in paragraphs
        if p
    )
    if action_url:
        url = escape(action_url, quote=True)
        label = escape(action_label or "Open")
        body += (
            '<table role="presentation" cellspacing="0" cellpadding="0" border="0" style="margin:8px 0 24px;">'
            f'<tr><td style="border-radius:8px;background:{ACCENT};">'
            f'<a href="{url}" style="display:inline-block;padding:12px 24px;font-family:{FONT};'
            f"font-size:15px;font-weight:600;color:{ACCENT_CONTRAST};text-decoration:none;"
            f'border-radius:8px;">{label}</a></td></tr></table>'
            f'<p style="margin:0 0 16px;font-size:13px;line-height:1.5;color:{TEXT_MUTED};">'
            "If the button doesn't work, copy this link into your browser:<br>"
            f'<a href="{url}" style="color:{ACCENT};word-break:break-all;">{url}</a></p>'
        )
    if footnote:
        body += (
            f'<p style="margin:0;font-size:13px;line-height:1.5;color:{TEXT_MUTED};">'
            f"{escape(footnote)}</p>"
        )

    return (
        "<!DOCTYPE html>"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{escape(heading)}</title></head>"
        f'<body style="margin:0;padding:0;background:{PAGE_BG};font-family:{FONT};">'
        f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:{PAGE_BG};">'
        '<tr><td align="center" style="padding:32px 16px;">'
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="max-width:560px;">'
        '<tr><td style="padding:0 4px 16px;">'
        f'<span style="font-family:{FONT};font-size:20px;font-weight:700;color:{ACCENT};letter-spacing:0.2px;">Rackflow</span>'
        "</td></tr>"
        f'<tr><td style="background:{CARD_BG};border:1px solid {BORDER};border-top:4px solid {ACCENT};'
        'border-radius:12px;padding:32px 28px;">'
        f'<h1 style="margin:0 0 20px;font-family:{FONT};font-size:20px;line-height:1.3;color:{TEXT};">'
        f"{escape(heading)}</h1>"
        f"{body}"
        "</td></tr>"
        f'<tr><td style="padding:16px 4px 0;font-size:12px;line-height:1.5;color:{TEXT_MUTED};">'
        "You're receiving this email because of activity on your Rackflow account."
        "</td></tr>"
        "</table></td></tr></table></body></html>"
    )
