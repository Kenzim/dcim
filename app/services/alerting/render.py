"""Turn alert notices into email text and webhook bodies (generic JSON, Discord, Slack)."""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any, Optional

from app.core.config import settings
from app.models.alerting import Alert, ChannelKind
from app.services.alerting.catalogue import naive

EVENT_LABELS = {
    "opened": "ALERT",
    "reminder": "STILL OPEN",
    "escalated": "ESCALATED",
    "resolved": "RESOLVED",
    "flapping": "FLAPPING",
}
SEVERITY_COLOURS = {"info": 0x3B82F6, "warning": 0xF59E0B, "critical": 0xDC2626}
RESOLVED_COLOUR = 0x16A34A
MAX_DETAIL_ITEMS = 12


def _iso(value: Optional[datetime]) -> Optional[str]:
    value = naive(value)
    return value.isoformat() + "Z" if value else None


def admin_link() -> Optional[str]:
    base = (settings.public_base_url or settings.public_app_url or "").strip().rstrip("/")
    return f"{base}/admin/alerts" if base else None


def snapshot(alert: Alert, event: str) -> dict[str, Any]:
    """Self-contained copy of an alert as it was when the notice was queued."""
    details = alert.details if isinstance(alert.details, dict) else {}
    return {
        "id": alert.id,
        "event": event,
        "type": alert.alert_type,
        "severity": alert.severity,
        "title": alert.title,
        "message": alert.message or "",
        "resource_type": alert.resource_type,
        "resource_id": alert.resource_id,
        "resource_label": alert.resource_label or "",
        "opened_at": _iso(alert.opened_at),
        "resolved_at": _iso(alert.resolved_at),
        "notify_count": alert.notify_count,
        "details": dict(list(details.items())[:MAX_DETAIL_ITEMS]),
    }


def subject_for(item: dict[str, Any]) -> str:
    label = EVENT_LABELS.get(item["event"], item["event"].upper())
    severity = item["severity"].upper()
    tag = label if item["event"] == "resolved" else f"{label} {severity}"
    return f"[Rackflow] [{tag}] {item['title']}"[:200]


def _lines(item: dict[str, Any]) -> list[str]:
    lines = [item["title"], ""]
    if item["message"]:
        lines += [item["message"], ""]
    lines.append(f"Severity: {item['severity']}")
    lines.append(f"Type: {item['type']}")
    if item["resource_label"]:
        lines.append(f"Resource: {item['resource_label']}")
    if item["opened_at"]:
        lines.append(f"Opened: {item['opened_at']}")
    if item["resolved_at"]:
        lines.append(f"Resolved: {item['resolved_at']}")
    return lines


def email_for_alert(item: dict[str, Any]) -> dict[str, str]:
    lines = _lines(item)
    link = admin_link()
    if link:
        lines += ["", f"Open the alert list: {link}"]
    text = "\n".join(lines)
    body = "".join(f"<p>{html.escape(line)}</p>" if line else "" for line in lines)
    return {"subject": subject_for(item), "text": text, "html": body}


def email_for_summary(items: list[dict[str, Any]]) -> dict[str, str]:
    lines = [f"{len(items)} alert notices were raised at the same time.", ""]
    for item in items:
        label = EVENT_LABELS.get(item["event"], item["event"])
        lines.append(f"- [{label}] [{item['severity']}] {item['title']}")
    link = admin_link()
    if link:
        lines += ["", f"Open the alert list: {link}"]
    text = "\n".join(lines)
    body = "<p>" + "</p><p>".join(html.escape(line) for line in lines if line) + "</p>"
    return {"subject": f"[Rackflow] {len(items)} alert notices", "text": text, "html": body}


def webhook_body(items: list[dict[str, Any]], *, event: str, sent_at: str, summary: bool) -> dict[str, Any]:
    return {
        "version": 1,
        "event": "summary" if summary else event,
        "sent_at": sent_at,
        "link": admin_link(),
        "alerts": [dict(i) for i in items],
    }


def _discord_embed(item: dict[str, Any]) -> dict[str, Any]:
    resolved = item["event"] == "resolved"
    fields = []
    if item["resource_label"]:
        fields.append({"name": "Resource", "value": item["resource_label"][:200], "inline": True})
    fields.append({"name": "Severity", "value": item["severity"], "inline": True})
    embed: dict[str, Any] = {
        "title": subject_for(item)[:256],
        "description": (item["message"] or item["title"])[:1800],
        "color": RESOLVED_COLOUR if resolved else SEVERITY_COLOURS.get(item["severity"], 0x6B7280),
        "fields": fields,
    }
    if item["opened_at"]:
        embed["timestamp"] = item["opened_at"] if not resolved else (item["resolved_at"] or item["opened_at"])
    link = admin_link()
    if link:
        embed["url"] = link
    return embed


def discord_body(items: list[dict[str, Any]], *, summary: bool) -> dict[str, Any]:
    if summary:
        lines = [f"{len(items)} alert notices were raised at the same time:"]
        lines += [f"- [{i['severity']}] {i['title']}" for i in items[:20]]
        return {"username": "Rackflow", "content": "\n".join(lines)[:1900]}
    return {"username": "Rackflow", "embeds": [_discord_embed(i) for i in items[:10]]}


def _slack_attachment(item: dict[str, Any]) -> dict[str, Any]:
    resolved = item["event"] == "resolved"
    colour = "#16a34a" if resolved else {"info": "#3b82f6", "warning": "#f59e0b", "critical": "#dc2626"}.get(
        item["severity"], "#6b7280"
    )
    fields = [{"title": "Severity", "value": item["severity"], "short": True}]
    if item["resource_label"]:
        fields.append({"title": "Resource", "value": item["resource_label"][:200], "short": True})
    return {
        "color": colour,
        "title": subject_for(item)[:200],
        "text": (item["message"] or item["title"])[:2500],
        "fields": fields,
        "fallback": subject_for(item),
    }


def slack_body(items: list[dict[str, Any]], *, summary: bool) -> dict[str, Any]:
    if summary:
        lines = [f"{len(items)} alert notices were raised at the same time:"]
        lines += [f"- [{i['severity']}] {i['title']}" for i in items[:20]]
        return {"text": "\n".join(lines)[:2900]}
    return {"text": subject_for(items[0]), "attachments": [_slack_attachment(i) for i in items[:10]]}


def channel_payload(kind: str, items: list[dict[str, Any]], *, sent_at: str, summary: bool) -> dict[str, Any]:
    """Build the stored delivery payload for a channel kind."""
    if kind == ChannelKind.EMAIL:
        return email_for_summary(items) if summary else email_for_alert(items[0])
    if kind == ChannelKind.DISCORD:
        return discord_body(items, summary=summary)
    if kind == ChannelKind.SLACK:
        return slack_body(items, summary=summary)
    return webhook_body(items, event=items[0]["event"], sent_at=sent_at, summary=summary)
