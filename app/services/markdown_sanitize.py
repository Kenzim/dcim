"""Minimal safe markdown-to-HTML conversion using stdlib only."""

from __future__ import annotations

import html
import re

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_CODE = re.compile(r"`([^`]+)`")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_HEADER = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
_UL = re.compile(r"^[\-\*]\s+(.+)$", re.MULTILINE)


def sanitize_markdown_to_html(md: str) -> str:
    if not md:
        return ""
    text = html.escape(md)

    def _link(match: re.Match[str]) -> str:
        label, url = match.group(1), match.group(2)
        return f'<a href="{url}" rel="noopener noreferrer">{label}</a>'

    text = _LINK.sub(_link, text)
    text = _CODE.sub(r"<code>\1</code>", text)
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    text = _ITALIC.sub(r"<em>\1</em>", text)
    text = _HEADER.sub(
        lambda m: f"<h{len(m.group(1))}>{m.group(2)}</h{len(m.group(1))}>",
        text,
    )
    text = _UL.sub(r"<li>\1</li>", text)
    text = re.sub(r"(?:<li>.*?</li>\s*)+", lambda m: f"<ul>{m.group(0)}</ul>", text)
    text = text.replace("\n\n", "</p><p>")
    text = text.replace("\n", "<br>")
    return f"<p>{text}</p>"
