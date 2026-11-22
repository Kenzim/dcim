"""Plugin template: replace the handlers below with your own."""


def register(plugin):

    @plugin.on("service.created")
    def created(ctx, event):
        # event["payload"] holds the fields listed for this event in the API reference.
        state = ctx.state(event["service_id"])
        state.set("message", f"{ctx.settings.get('greeting')}! Your service is ready.")

    @plugin.action("say_hello")
    def say_hello(ctx, request):
        state = ctx.state(request["service"]["id"])
        name = (request["params"].get("name") or "").strip() or "there"
        state.set("message", f"{ctx.settings.get('greeting')}, {name}!")
        state.set("hellos", int(state.get("hellos", 0)) + 1)
        return {"message": "Hello said."}
