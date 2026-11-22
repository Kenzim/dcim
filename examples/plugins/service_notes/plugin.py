"""A label and a note per service, shown in a panel and edited through actions."""

from datetime import datetime, timedelta

NOTE_KEYS = ("label", "note", "updated_at", "updated_by", "review_status")


def parse(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def register(plugin):

    @plugin.action("save_note")
    def save_note(ctx, request):
        params = request["params"]
        label = (params.get("label") or "").strip()
        note = (params.get("note") or "").strip()
        if not label and not note:
            raise ctx.ActionError("Enter a label or a note, or use Clear note")
        service_id = request["service"]["id"]
        state = ctx.state(service_id)
        state.set("label", label)
        state.set("note", note)
        state.set("updated_at", ctx.now().isoformat())
        state.set("updated_by", request["actor"]["type"])
        state.set("review_status", "current")
        ctx.audit("note_saved", service_id=service_id, details={"by": request["actor"]["type"]})
        return {"message": "Saved."}

    @plugin.action("clear_note")
    def clear_note(ctx, request):
        service_id = request["service"]["id"]
        state = ctx.state(service_id)
        for key in NOTE_KEYS:
            state.delete(key)
        ctx.audit("note_cleared", service_id=service_id, details={"by": request["actor"]["type"]})
        return {"message": "Note removed."}

    @plugin.action("mark_reviewed")
    def mark_reviewed(ctx, request):
        state = ctx.state(request["service"]["id"])
        state.set("updated_at", ctx.now().isoformat())
        state.set("review_status", "current")
        return {"message": "Marked as reviewed."}

    @plugin.schedule("review")
    def review(ctx, service):
        state = ctx.state(service["id"])
        changed = parse(state.get("updated_at"))
        if changed is None or state.get("review_status") == "stale":
            return
        limit = timedelta(days=int(ctx.settings.get("review_after_days") or 90))
        if ctx.now() - changed < limit:
            return
        state.set("review_status", "stale")
        if ctx.settings.get("notify_owner"):
            ctx.notify.client(
                service["id"],
                f"The note on your service {service['name']} has not changed for a while. Please check it is still right.",
                key=f"review:{service['id']}:{state.get('updated_at')}",
            )

    @plugin.mcp_tool("get_note")
    def get_note(ctx, params):
        state = ctx.state(params["service_id"])
        return {"label": state.get("label"), "note": state.get("note"), "review_status": state.get("review_status")}
