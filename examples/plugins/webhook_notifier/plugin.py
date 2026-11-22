"""Posts a one-line message to a Discord or Slack incoming webhook for a few events."""

SEEN_KEY = "seen_event_ids"
SEEN_LIMIT = 200
RETRY_STATUSES = (408, 425, 429)


def money(cents, currency):
    if cents is None:
        return ""
    return f"{int(cents) / 100:.2f} {currency or ''}".strip()


def clean(text, limit=200):
    """Keep chat mentions and markup in ticket subjects from doing anything."""
    text = str(text or "").replace("\n", " ").strip()[:limit]
    for char, entity in (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;")):
        text = text.replace(char, entity)
    return text.replace("@", "@\u200b")


def build_body(kind, text):
    if kind == "slack":
        return {"text": text}
    return {"content": text, "allowed_mentions": {"parse": []}}


async def post(ctx, text):
    url = str(ctx.settings.get("webhook_url") or "")
    if not url.startswith("https://"):
        ctx.log.warning("Webhook URL is not set; nothing was posted")
        return
    reply = await ctx.http.post(url, json=build_body(ctx.settings.get("format"), text))
    status = reply["status"]
    if status >= 500 or status in RETRY_STATUSES:
        raise RuntimeError(f"The chat service answered {status}; will retry")
    if status >= 400:
        ctx.log.error(f"The chat service refused the message with status {status}; check the webhook URL")


def first_delivery(ctx, event):
    """Record the event id, and say whether this is the first time it is seen."""
    seen = list(ctx.store.get(SEEN_KEY, []))
    if event["id"] in seen:
        return False
    ctx.store.set(SEEN_KEY, (seen + [event["id"]])[-SEEN_LIMIT:])
    return True


def register(plugin):

    @plugin.on("ticket.created")
    async def ticket_created(ctx, event):
        if not ctx.settings.get("notify_tickets") or not first_delivery(ctx, event):
            return
        payload = event["payload"]
        await post(ctx, f"New ticket {payload.get('ticket_number')}: {clean(payload.get('subject'))}")

    @plugin.on("invoice.paid")
    async def invoice_paid(ctx, event):
        if not ctx.settings.get("notify_invoices") or not first_delivery(ctx, event):
            return
        payload = event["payload"]
        number = payload.get("number_display") or payload.get("invoice_number") or payload["invoice_id"]
        amount = money(payload.get("total_cents"), payload.get("currency"))
        await post(ctx, f"Invoice {number} was paid" + (f": {amount}" if amount else ""))

    @plugin.on("service.suspended")
    async def service_suspended(ctx, event):
        if not ctx.settings.get("notify_suspensions") or not first_delivery(ctx, event):
            return
        service = ctx.services.get(event["service_id"])
        await post(ctx, f"Service {clean(service['name'])} ({service['service_type']}) was suspended")
