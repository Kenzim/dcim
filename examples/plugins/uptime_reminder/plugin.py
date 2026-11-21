"""Uptime reminder: a small example plugin for the Rackflow plugin API 1.0."""

from datetime import datetime, timedelta


def _parse(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def register(plugin):

    def start_period(ctx, state):
        now = ctx.now()
        days = int(ctx.settings.get("reminder_days", 30))
        state.set("status", "ok")
        state.set("confirmed_at", now.isoformat())
        state.set("due_at", (now + timedelta(days=days)).isoformat())

    async def mark_due(ctx, service):
        state = ctx.state(service["id"])
        state.set("status", "due")
        state.set("reminders_sent", int(state.get("reminders_sent", 0)) + 1)
        if ctx.settings.get("notify_clients", True):
            ctx.notify.client(
                service["id"],
                f"Please confirm that you still need your service {service['name']}.",
                key=f"due:{state.get('due_at')}",
            )
        url = ctx.settings.get("webhook_url")
        if url:
            headers = {}
            token = ctx.settings.get("webhook_token")
            if token:
                headers["Authorization"] = f"Bearer {token}"
            try:
                await ctx.http.post(url, json={"event": "reminder_due", "service_id": service["id"]}, headers=headers)
            except ctx.errors.PluginError as exc:
                ctx.log.warning(f"Webhook failed: {exc}", service_id=service["id"])
        ctx.log.info("Reminder due", service_id=service["id"])

    @plugin.on("service.created")
    def created(ctx, event):
        start_period(ctx, ctx.state(event["service_id"]))

    @plugin.on("service.suspended")
    def suspended(ctx, event):
        ctx.state(event["service_id"]).set("status", "paused")

    @plugin.on("service.unsuspended")
    def unsuspended(ctx, event):
        start_period(ctx, ctx.state(event["service_id"]))

    @plugin.schedule("check")
    async def check(ctx, service):
        if service is None or service["status"] != "active":
            return
        state = ctx.state(service["id"])
        status = state.get("status")
        if status is None or status == "paused":
            start_period(ctx, state)
            return
        due_at = _parse(state.get("due_at"))
        if status == "ok" and due_at is not None and due_at <= ctx.now():
            await mark_due(ctx, service)

    @plugin.action("confirm")
    def confirm(ctx, request):
        state = ctx.state(request["service"]["id"])
        start_period(ctx, state)
        note = (request["params"].get("note") or "").strip()
        if note:
            state.set("note", note)
        else:
            state.delete("note")
        return {"message": "Thanks, we have recorded that you still need this service."}

    @plugin.action("remind_now")
    async def remind_now(ctx, request):
        await mark_due(ctx, request["service"])
        return {"message": "The client has been asked to confirm."}

    @plugin.mcp_tool("status")
    def status(ctx, params):
        state = ctx.state(params["service_id"]).all()
        return {
            "service_id": params["service_id"],
            "status": state.get("status"),
            "confirmed_at": state.get("confirmed_at"),
            "due_at": state.get("due_at"),
            "reminders_sent": state.get("reminders_sent", 0),
        }
