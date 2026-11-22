# Rackflow plugin author guide

This guide takes you from an empty folder to a plugin that an admin has installed and enabled. It is for plugin API **1.0**. Three other documents go with it:

- the [API reference](api-reference.md), generated from the code: every permission, event and payload field, manifest key, SDK member and limit;
- the [testing guide](testing.md), for the pytest test kit;
- the [deprecation policy](deprecation-policy.md) and the [changelog](CHANGELOG.md), for what may change and how you will hear about it.

[The plugin system design](../plugin-system.md) is the full contract, including the reasoning behind it. When this guide and the contract differ, the contract wins; please report the difference.

## 1. What a plugin is

A plugin is a folder with a manifest (`plugin.json`) and a Python entry module. An admin installs it from Rackflow's plugins directory, approves the permissions it asks for, and enables it. A plugin can:

- react to events (a ticket was opened, an invoice was paid, a service was suspended);
- refuse a power action before it happens;
- run on a schedule;
- keep settings (with encrypted secrets) and per-service state;
- show panels with fields, badges and buttons on the service page of the admin interface, the client portal and WHMCS, without sending any HTML;
- offer tools to staff and client MCP clients;
- act on the world through the SDK: send an HTTP request to a public address, notify a client, run a command in a VM, power a VM.

A plugin is trusted code running inside the Rackflow process. It reaches Rackflow only through the `ctx` object it is handed, and `ctx` enforces the permissions you declared. Do not import `app.*`; it may work today and will break without notice.

Only admins install, configure, upgrade and remove plugins. Clients never install anything; at most they see a panel on their own service and press the buttons you offer them.

## 2. Your first plugin in ten minutes

Copy the template and rename it. The folder name is the plugin id: lowercase letters, digits and underscores, starting with a letter, 3 to 48 characters.

```bash
cp -r examples/plugins/plugin_template examples/plugins/hello_world
cd examples/plugins/hello_world
```

Edit `plugin.json`: set `"id": "hello_world"` (it must equal the folder name), a name, a description, an author and a licence. Edit `plugin.py`. A complete, working entry module is this small:

```python
def register(plugin):

    @plugin.on("service.created")
    def created(ctx, event):
        ctx.state(event["service_id"]).set("message", f"{ctx.settings.get('greeting')}! Your service is ready.")
```

`register(plugin)` binds handlers to the names declared in the manifest. Every handler you bind must be declared in `plugin.json`, and every declared one must be bound; the host refuses the package otherwise.

Test it (from the root of a Rackflow checkout, with its Python dependencies installed):

```bash
pytest examples/plugins/hello_world/tests
```

The test installs the plugin into a throwaway in-memory Rackflow, creates a service and checks the state the handler wrote. See [testing](testing.md).

Install it on a real Rackflow:

1. Put the folder in the plugins directory (`PLUGINS_DIR`, default `plugins` next to where Rackflow runs; in Docker mount a volume there). Only the files of the package: leave out `tests/` if you like, it is ignored.
2. Open the admin interface, the Plugins page. The package is listed with its permissions and risk levels, or with its errors when it is invalid.
3. Press Install, check the permissions, accept them, and enable. Or through the API, as an admin: `POST /api/admin/plugins/install` with `{"directory": "hello_world", "accept_permissions": ["service.read"], "enable": true}`.
4. Fill in the settings on the plugin's page. Secrets are write-only.

## 3. The manifest

`plugin.json` is validated by the host before anything else runs. Unknown keys are refused, so a typo is an error, not a silent no-op. The [reference](api-reference.md#manifest-pluginjson) lists every key with its type and limits; this section explains how they fit together.

```json
{
  "id": "hello_world",
  "name": "Hello world",
  "version": "1.0.0",
  "description": "Greets new services.",
  "author": "Your name",
  "licence": "MIT",
  "rackflow_api": ">=1.0,<2.0",
  "entry": "plugin.py",
  "permissions": ["service.read"],
  "services": {"types": ["vm"], "enablement": "all"},
  "settings": [{"key": "greeting", "label": "Greeting", "type": "string", "default": "Welcome"}],
  "events": ["service.created"]
}
```

- **`id`** must equal the folder name and never changes. **`version`** is `MAJOR.MINOR.PATCH`; raise it for every release, because Rackflow refuses downgrades and offers an upgrade only when the version on disk is higher.
- **`rackflow_api`** is the range of plugin API versions you support: `>=1.0,<2.0` for a plugin written for 1.0 and expected to work on every 1.x. Always give the upper bound. See [the deprecation policy](deprecation-policy.md).
- **`permissions`** must list everything you use (section 4). The host checks that events, hooks, panels and tools have the permission they need.
- **`services`** is required when you have actions, panels or per-service schedules, and for any service event. `types` are the service types you apply to (`vm`, `bare_metal`, `http_proxy`, `nfs_storage`, `backup_slot`). `enablement` says when the plugin is active for a service:
  - `all`: every service of those types;
  - `opt_in` (the default): only services an admin switched it on for, or that the plugin switched on itself with `ctx.set_active`; an admin's decision always wins;
  - `addon`: services that bought a billing add-on linked to the plugin on its admin page.
- **`events`** are delivered to after handlers; **`hooks`** are events with before hooks.
- **`schedules`**, **`actions`**, **`ui`** and **`mcp_tools`** are described below.
- **`tables`** is reserved and refused in API 1.0.

A service event reaches the plugin only for services it is active for. With `opt_in`, a plugin that has not been switched on for a service sees none of that service's events, including `service.created`; use `all` when you want to see every new service.

## 4. Permissions

Declare only what you use. The admin sees the list, with a plain description and a risk level, and must accept all of it; at upgrade they must accept any permission you added. Each permission gates both the manifest and the SDK, so a call without its permission raises `ctx.errors.PermissionDenied`.

| Permission | Risk | You need it to |
|---|---|---|
| `service.read` | low | receive `service.*` events; use `ctx.services` and `ctx.set_active` |
| `billing.read` | low | receive order, invoice, credit note, refund and credit events |
| `support.read` | low | receive `ticket.created` |
| `events.veto` | medium | declare `hooks` and refuse operations |
| `notify.client` | medium | `ctx.notify.client` |
| `http.outbound` | medium | `ctx.http.get` and `ctx.http.post` |
| `ui.client` | medium | show panels and buttons to clients |
| `mcp.staff`, `mcp.client` | medium | offer MCP tools to staff or clients |
| `vm.power` | high | `ctx.vm.power` |
| `guest.exec` | high | `ctx.guest.exec`, `ctx.guest.ping` |

The exact wording admins see is in the [reference](api-reference.md#permissions). Ask for the least you can. A plugin that wants `guest.exec` and `vm.power` will be read carefully before it is approved.

## 5. Settings and secrets

Settings are the form an admin fills in on the plugin's page. Declare them in `settings`; read them in handlers with `ctx.settings.get("key", default)`.

| `type` | Value |
|---|---|
| `string`, `text` | text; `max_length`, `pattern` |
| `integer`, `number` | numbers; `min`, `max` |
| `boolean` | true or false |
| `select` | one of `options[].value` |
| `url` | an `http` or `https` URL |
| `secret` | write-only text, stored encrypted |
| `window` | days, start, end and time zone; test with `ctx.in_window("key")` |

The host validates values on save and fills in defaults at install and upgrade. A `secret` is sealed with Rackflow's encryption key, shown to the admin only as a mask and decrypted only for your handlers through `ctx.settings`. Never write a secret to the log, to state or to a panel. Settings you remove from a later version are kept hidden until the plugin is uninstalled.

## 6. State and storage

There is no database access. A plugin keeps data in key-value state, JSON values, in two scopes:

- `ctx.state(service_id)`: per service. The plugin must be active for that service, or `NotActive` is raised. This is what panels read.
- `ctx.store`: plugin-wide.

Both have `get(key, default)`, `set(key, value)`, `delete(key)`, `all()` and `keys(prefix)`. Keys match `^[A-Za-z0-9_.:-]{1,128}$`; a value is at most 64 KB and a plugin has at most 100,000 keys. Writes are committed when the handler returns successfully; if it raises, they are rolled back. State is deleted with the service, and with the plugin unless the admin uninstalls with "keep data".

Plugin-owned tables are not part of API 1.0.

## 7. Events, before hooks and delivery

Subscribe with `@plugin.on("event.name")` after declaring the name in `events`. The handler receives `ctx` and the envelope:

```json
{"id": "6f1c...", "name": "invoice.paid", "version": "1.0", "occurred_at": "2026-11-21T10:00:00Z", "service_id": null, "payload": {"invoice_id": 301, "billing_account_id": 7}}
```

The [reference](api-reference.md#events) lists each event with its required and optional payload fields. Required fields are always present; optional ones may be missing or null; fields you do not know may be added in later minor versions, so read what you need and ignore the rest.

After handlers are delivered **at least once**, through a durable queue:

- a handler that raises is retried with a back-off (30 seconds doubling, up to an hour) and goes dead after 10 attempts, which an admin can retry;
- the same event can arrive twice after a crash or a timeout, so make handlers idempotent, for example by remembering `event["id"]` in state;
- a handler has 30 seconds; a plain `def` handler runs in a small thread pool, an `async def` handler on the event loop and must not block;
- events for a service the plugin is no longer active for are skipped.

A **before hook** runs inside the request, before an operation, and can refuse it. API 1.0 has one: `service.power_requested`. Declare it under `hooks` (it needs `events.veto` as well as `service.read`):

```python
@plugin.before("service.power_requested")
def no_stop_during_backup(ctx, event):
    if event["payload"]["action"] == "off" and ctx.state(event["service_id"]).get("backup_running"):
        return ctx.veto("A backup is running; try again in a few minutes")
```

Return `None` to allow. The caller gets HTTP 409 with your reason prefixed by the plugin name, and the veto is audited. Hooks have 2 seconds each and 3 in total; a hook that times out or raises is treated as "allow", so a broken plugin cannot lock clients out of their servers. `ctx.alter({...})` changes mutable payload fields; no event has any in API 1.0.

## 8. Schedules

```json
"schedules": [{"id": "check", "every": "1h", "scope": "service", "timeout": 60, "jitter": "10m"}]
```

```python
@plugin.schedule("check")
async def check(ctx, service):
    ...   # service is a dict for scope "service", None for scope "global"
```

`every` is at least 60 seconds, as a number of seconds or with `s`, `m`, `h` or `d`. A `global` schedule runs once per interval; a `service` schedule runs once for every service the plugin is active for, spread over `jitter` so they do not start together. A schedule that is late (Rackflow was down) runs once, not once per missed interval. The service dict has `id`, `name`, `service_type`, `status`, `owner_user_id`, `product_code`, `os_code` and `created_at`. Several Rackflow processes share the work safely; each run is leased to one.

## 9. The SDK

Every handler receives `ctx`. The [reference](api-reference.md#sdk-context) lists every member with its signature and permission. In practice:

| You want to | Use |
|---|---|
| read a setting | `ctx.settings.get("key", default)` |
| keep data | `ctx.state(service_id)`, `ctx.store` |
| know the time | `ctx.now()` (UTC, timezone aware), `ctx.in_window("setting")` |
| write to the plugin log | `ctx.log.info("message", service_id=..., key=value)` (also debug, warning, error) |
| write to the audit log | `ctx.audit("action", service_id=..., details={...})`, stored as `plugin.<id>.<action>` |
| read service facts | `ctx.services.get(id)`, `ctx.services.active("vm")` |
| call a public web address | `await ctx.http.post(url, json=..., headers=...)`, `await ctx.http.get(url)`; returns `{"status": int, "text": str}` |
| tell a client | `ctx.notify.client(service_id, "text", key="stable-key")`; the key de-duplicates; the client must have opted in to plugin notices |
| run a command in a VM | `await ctx.guest.exec(service_id, ["cmd", "arg"])` returns `{"exit_code", "stdout", "stderr"}`; `await ctx.guest.ping(service_id)` |
| power a VM | `await ctx.vm.power(service_id, "reboot")` (`on`, `off`, `shutdown`, `reboot`, `reset`) |
| refuse a refused operation | `ctx.veto(reason)` from a before hook; `raise ctx.ActionError(message)` from an action |

Things to know:

- **HTTP** is limited to public addresses and HTTPS (an operator can allow plain HTTP). Requests to private, loopback and link-local addresses are refused, redirects are not followed, and answers are cut at 64 KB. Every request is audited with its method, host and status, never its body. A network failure raises an exception; catch it where a failure is not fatal.
- **Commands and power actions** are rate limited and audited. `ctx.guest.exec` takes a list of arguments, not a shell string. Pass `sensitive=True` when the arguments hold something secret: the audit entry then records the command name and a hash of the rest instead of the arguments.
- **Network calls are coroutines.** Await them from `async def` handlers. Storage, settings, logging and notices are plain methods usable from both kinds.
- **Errors** are in `ctx.errors`: `PermissionDenied`, `RateLimited`, `NotActive`, `GuestAgentError`, all subclasses of `PluginError`.
- Lifecycle handlers (`@plugin.lifecycle("install")` and `enable`, `disable`, `upgrade`, `uninstall`) get `ctx` and an `info` dict (for example `{"from_version": ..., "to_version": ...}` on upgrade). A failing `install` aborts the install; a failing `upgrade` marks the plugin failed.

## 10. Panels and actions (declarative UI)

You describe a panel in the manifest; Rackflow evaluates it against your per-service state and serves one JSON render document that the admin interface, the client portal, WHMCS and the MCP servers all draw. Rendering never runs your code, so a slow plugin cannot slow a service page. You write no HTML, JavaScript or PHP.

```json
"actions": [
  {"id": "confirm", "label": "Confirm I still need this", "audience": ["client", "admin"],
   "confirm": "Confirm that you still use this service?", "style": "primary",
   "params": [{"key": "note", "label": "Note", "type": "string", "max_length": 200, "default_state": "note"}],
   "when": {"state": "status", "eq": "due"}}
],
"ui": {"panels": [{
  "id": "status", "title": "Uptime reminder", "placement": "service", "audience": ["admin", "client"],
  "elements": [
    {"type": "badge", "label": "Status", "state": "status",
     "map": {"ok": {"text": "Confirmed", "tone": "success"}, "due": {"text": "Please confirm", "tone": "warning"}},
     "default": {"text": "Not checked yet", "tone": "neutral"}},
    {"type": "field", "label": "Last confirmed", "state": "confirmed_at", "format": "datetime", "empty": "Never"},
    {"type": "text", "text": "We ask every 30 days so unused services can be cancelled.", "audience": ["client"]},
    {"type": "action", "action": "confirm"}
  ]}]}
```

```python
@plugin.action("confirm")
def confirm(ctx, request):
    # request: {"service": {...}, "action": "confirm", "params": {...}, "actor": {...}}
    ctx.state(request["service"]["id"]).set("status", "ok")
    return {"message": "Thanks, confirmed."}
```

- **Elements**: `field` (a label and a value from `state` or `value`, with a `format`: `text`, `number`, `boolean`, `date`, `datetime`, `duration`, `bytes`), `badge` (a coloured status, from a `map` of state values to text and tone), `text` (a paragraph, or a notice box when it has a `tone`), `action` (a button, or a small form when the action has params) and `divider`. Tones: `success`, `warning`, `danger`, `info`, `neutral`.
- **`when`** on a panel, element or action shows or allows it only when a state condition holds: `truthy`, `falsy`, `eq`, `ne` or `in`. An action whose `when` is false is refused, not just hidden.
- **`audience`**: `admin` and `client`. `client` needs `ui.client`. A client can run an action only on their own services; staff on any.
- **`confirm`** text makes every caller confirm; the API refuses a request without `confirm: true`.
- **Params** are validated by the host before your handler runs; they use the setting types except `secret` and `window`. `default_state` prefills a field from per-service state, which is how a panel doubles as a per-service settings form.
- **Results**: return `{"message": "..."}`. Raise `ctx.ActionError("text")` to refuse with a message the user sees (HTTP 409). Any other exception is a failure that counts against the plugin's health and shows the user a generic message. After an action Rackflow returns the refreshed panels, so the page updates without a reload.
- **Limits**: 20 panels per plugin, 40 elements per panel, strings up to 500 characters (text elements 2,000). An action has 15 seconds by default and at most 30; long work belongs in a schedule, with the action recording the request in state. Ten calls a minute per caller, action and service.

### Where panels appear

| Surface | How it renders the document |
|---|---|
| Admin interface | Service page, "Plugins" section, for panels with the `admin` audience |
| Client portal | Service page, for panels with the `client` audience, with only that client's own services |
| WHMCS | The service's client area and its admin services tab, through the module's generic renderer and `plugin_action.php`; no per-plugin code |
| Staff MCP | `plugins(action="panel", service_id=)` returns the admin document |
| Client MCP | `services(view="plugins", service_id=)` returns the client document and the client tools |

Surfaces skip element types they do not know, so a new element type in a later minor version cannot break an older renderer.

## 11. MCP tools

A plugin can offer tools to MCP clients without growing the tool list: staff reach them through the fixed `plugins` tool and clients through `service_action`. Declare them in `mcp_tools` (`audience` `staff` needs `mcp.staff`, `client` needs `mcp.client`; `scope` `read`, `write` or `destructive`; `params` as for actions) and bind them with `@plugin.mcp_tool("id")`. The handler receives `ctx` and the validated params and returns JSON-serialisable data. A `service_id` parameter makes the tool service scoped: the host checks that the plugin is active for it and, for clients, that they own it. Keep descriptions under 300 characters.

## 12. Failure isolation

Assume your plugin will fail sometimes; Rackflow is built so that it cannot hurt anything else.

- Every call into your code has a time limit (before hooks 2 s, actions up to 30 s, deliveries 30 s, schedules up to 600 s). Async handlers are cancelled at the limit; a blocking plain function cannot be killed, so Rackflow stops waiting and counts a failure.
- Exceptions are caught at the boundary, logged to the plugin log with a trimmed traceback, and shown to admins as the last error.
- After 5 failures in a row the **circuit opens** for 60 seconds, doubling up to 30 minutes: deliveries and schedules wait, before hooks are skipped, actions and tools are refused with "temporarily unavailable". The first success closes it.
- After 25 failures in a row the plugin is **disabled** (status `failed`), an audit entry is written and staff are alerted. Re-enabling resets the counts.
- A package that fails to import is marked failed; other plugins carry on.

Write handlers so that a failure leaves nothing half done, catch the failures you expect (a webhook that is down), log them with `ctx.log.warning`, and let the unexpected ones raise: that is what the retry and the health display are for.

## 13. Testing

Use the test kit; it runs your plugin against the real host with no Rackflow install: [testing guide](testing.md). At minimum, give every plugin a test that calls `assert_valid_package(PLUGIN)` and one test for each handler, action and schedule. The examples show the pattern: [`webhook_notifier`](../../examples/plugins/webhook_notifier/tests/test_webhook_notifier.py), [`service_notes`](../../examples/plugins/service_notes/tests/test_service_notes.py) and [`uptime_reminder`](../../examples/plugins/uptime_reminder/tests/test_uptime_reminder.py).

## 14. Packaging and upgrading

- **Package**: the folder, named by the plugin id, with `plugin.json`, the entry module and any helper modules (imported relatively: `from . import helpers`). Rackflow imports the entry module as `rackflow_plugins.<id>`. Ship a README for humans; Rackflow does not show it. Distribute the folder as a zip or a repository; the admin unpacks it into the plugins directory.
- **Pure Python, no installs.** Rackflow never runs `pip` for a plugin. Use the standard library and what Rackflow already ships.
- **Files are pinned.** Rackflow hashes the package when the admin approves it. If a file changes on disk, the plugin stops loading and shows "files changed" until an admin reviews and upgrades, so code nobody approved never runs. Do not edit an installed plugin in place; change the version and replace the folder.
- **Upgrading.** Raise `version`, replace the folder, and on the admin page choose Upgrade (or `POST /api/admin/plugins/{id}/upgrade` with `accept_permissions`). If you added a permission the admin must accept it. Existing settings keep their values; new settings get their defaults; state is kept; your `upgrade` lifecycle handler runs with `from_version` and `to_version` and is the place to migrate state. Downgrades are refused.
- **Uninstalling** disables the plugin, runs your `uninstall` handler, and deletes its settings, state and logs, or keeps settings and state when the admin chooses "keep data".
- **Compatibility**: keep `rackflow_api` honest and read the [changelog](CHANGELOG.md) when Rackflow moves to a new API version.

## 15. Plugin kinds (not available yet)

<!-- PLUGIN KINDS: a later API version adds typed plugin kinds (the `provides` manifest section). Document them here, one subsection per kind, when they exist in the code and the reference. -->

API 1.0 plugins react to events, run schedules and add panels. They cannot yet *provide* a service that Rackflow calls, such as a tax number validator, a currency rate source or a notification channel. These need a typed plugin kind and a contract for each, which the design reserves (section 17.1 of [the design](../plugin-system.md)) but which does not exist. Do not try to emulate one with events. Plugins that need a kind, for example one that fetches European Central Bank currency rates, come once kinds exist.

## 16. Checklist before you share a plugin

- `assert_valid_package` passes with no warnings.
- Every permission you declare is used, and every call you make has its permission.
- Secrets are `secret` settings and never reach logs, state or panels.
- Handlers are idempotent and tolerate payload fields they do not know.
- Failures you expect are caught and logged; the plugin works with its settings empty (it does nothing, and says why in the log) rather than crashing on every event.
- `rackflow_api` has a lower and an upper bound; `version` was raised.
- The README says what the plugin does, its permissions and why it needs each, and its settings.
