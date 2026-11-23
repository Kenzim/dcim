# Testing a plugin

The test kit lets you test a plugin against the real plugin host with `pytest`, without a Rackflow install, a database server or Redis. It lives in `app/plugin_host/testkit` and uses the host's own code for everything between "an event is queued" and "your handler ran": install and enable, the event bus and outbox, the scheduler, panel rendering, action and MCP tool dispatch, settings with encrypted secrets, state, logs, the circuit breaker, permission checks and the SDK context. Only the outside world is replaced. The database is SQLite in memory, Redis is a counter, `ctx.http` goes to a fake that records requests (after the host's own address and HTTPS checks), and VMs are a fake you script.

## Set up

Run your tests from a checkout of Rackflow, with its Python dependencies installed, and put the checkout on the path:

```bash
cd rackflow
pip install -r requirements.txt
PYTHONPATH=. pytest path/to/your_plugin/tests
```

A plugin that lives in the Rackflow repository, under `examples/plugins`, needs no `PYTHONPATH`: `pytest.ini` lists `examples` in `testpaths` and puts the repository root on the path, so a bare `pytest` also runs every example's tests. The [template](../../examples/plugins/plugin_template/README.md) has the files below ready to copy.

In `tests/conftest.py` of your plugin folder:

```python
from app.plugin_host.testkit.fixtures import rackflow  # noqa: F401
```

That gives each test a fresh `rackflow` object: an empty in-memory Rackflow with a plugins directory in a temporary folder.

## A first test

```python
from pathlib import Path

from app.plugin_host.testkit import assert_valid_package

PLUGIN = Path(__file__).resolve().parents[1]


def test_package_is_valid():
    assert_valid_package(PLUGIN)


def test_new_service_gets_a_welcome_note(rackflow):
    plugin = rackflow.install(PLUGIN)          # real install, permissions accepted, enabled
    service = rackflow.make_service()          # inserts a Service; the host queues service.created
    rackflow.deliver()                         # runs the outbox, as the plugin worker does
    assert plugin.state(service)["welcome"] == "sent"
```

## Checking the package

`check_package(path)` reads the folder the way an install does and returns a report; `assert_valid_package(path)` raises `AssertionError` listing every problem. They report:

- manifest errors, as the admin would see them: unknown keys, a bad id, an unknown permission or event, a permission the manifest does not declare but its events, hooks, panels or tools need, undeclared actions in a panel;
- a folder name that differs from the plugin id;
- a `rackflow_api` range that allows none of the supported API versions;
- a `provides` entry whose kind permission is missing, a `rackflow_api` range that allows only 1.0 together with `provides`, a pack file that is missing or invalid, and a provider that is declared but not registered with `@plugin.provider` (or the reverse), or whose object lacks a method of its kind;
- a missing entry module, an import error, an error in `register(plugin)`;
- a handler declared in the manifest but never registered, or registered but not declared;
- warnings: a missing description, author or licence, a `rackflow_api` range with no upper bound, and every [deprecated item](deprecation-policy.md) the package uses.

`report.supports("2.0")` tells you whether the range admits another API version, which is how you check a range before a new major version ships.

## The `rackflow` object

### Installing

`rackflow.install(folder, settings=None, enable=True, accept_permissions=None)` copies the folder into the temporary plugins directory (leaving out `tests`, `conftest.py` and caches) under its own name and runs the real install. It accepts every permission the manifest asks for unless you pass a shorter list, in which case it raises `PermissionsNotAccepted`. Settings are saved before the plugin is enabled, so an `enable` lifecycle handler already sees them. It returns an `InstalledPlugin`:

| Member | What it does |
|---|---|
| `plugin.configure(**values)` | Save settings through the admin screen's validation; raises on a bad value. Returns the keys that changed. |
| `plugin.settings()` | The settings as the admin API shows them (secrets masked). |
| `plugin.secret(key)` | A secret's stored value, for assertions. |
| `plugin.state(service=None)` | The plugin's key-value state, plugin-wide or for a service. |
| `plugin.logs(level=None)`, `plugin.log_messages(level=None)` | What the plugin wrote with `ctx.log`, plus the host's own failure lines. |
| `plugin.activate(service, on=True)` | The admin switching the plugin on or off for one service. Needed for plugins with `enablement` `opt_in`. |
| `plugin.status`, `plugin.installation` | The installation row: status, version, health counters, `last_error`. |
| `plugin.enable()`, `plugin.disable()`, `plugin.upgrade(source=None, accept_permissions=None)`, `plugin.uninstall(keep_data=False)` | The lifecycle, through the real code. `upgrade` copies a newer folder over the installed files first. |

### People and services

- `rackflow.make_user(username=None, admin=False, plugin_notices=True)`: a user. A client has opted in to plugin notices by default, as `ctx.notify.client` needs.
- `rackflow.make_service(owner=None, type="vm", status="active", name=None, product_code=None, os_code=None)`: inserts a service the way Rackflow does, so plugins subscribed to `service.created` receive a real event.
- `rackflow.set_status(service, "suspended")`: changes status; the host's listener queues `service.suspended`, `service.unsuspended`, `service.activated` or `service.terminated`.
- `rackflow.client_notices(user)`: the notices queued for a client by `ctx.notify.client`.

### Events and before hooks

- `rackflow.fire(name, service=None, **fields)` queues an event and delivers it at once. It returns `Fired`: `queued`, `deliveries`, and the shortcuts `delivered`, `failed`, `skipped` and `errors`. Fields you leave out get sample values from `sample_payload`; a field name the event does not have raises `KeyError`.
- `rackflow.emit(...)` and `rackflow.deliver()` do the two halves separately. Use them to see what is queued first, or to test retries; `rackflow.deliveries(plugin_id=None, event=None, status=None)` lists the outbox rows with their status, attempts and last error.
- `rackflow.request_power(service, "reboot", source="admin")` runs the power before hooks and returns the veto message, or `None` when nothing refused.

A failing handler is delivered as `failed` and retried later with a back-off, exactly as in production; the plugin's `consecutive_failures` counts it. Call `rackflow.fire` repeatedly to test the circuit breaker.

### Schedules

`rackflow.run_schedule(plugin, "check", service=None)` makes the schedule due, runs it through the real scheduler and returns a list of `ScheduleRun` (`status` is `"ok"` or `"failed"`, plus `error`). For a per-service schedule pass the service, or leave it out to run every active service.

### Time

`rackflow.clock.set(datetime)`, `rackflow.clock.advance(days=11)` and `rackflow.clock.reset()` change what `ctx.now()` and `ctx.in_window()` see. The host's own timers (retry back-off, circuit breaker, schedule due times) keep real time, which is why `run_schedule` forces a schedule due instead of waiting.

### Panels, actions and tools

- `rackflow.panels(service, audience="client")` is the document the portal (`"client"`) or the admin interface (`"admin"`) renders, with the panel's elements already resolved from state. `rackflow.panel(service, "panel_id", audience)` returns one panel.
- `rackflow.run_action(plugin, service, "action_id", params=None, as_="admin", confirm=False)` runs an action as an admin or as the service's client. It returns `{"ok": True, "message": ..., "panels": [...]}`, or raises `PluginRequestError` with the status code the API would send: 400 for bad parameters or a missing confirmation, 403 for the wrong audience, 409 when the handler raised `ctx.ActionError`, 503 when it failed.
- `rackflow.run_tool(plugin, "tool_id", params, audience="staff")` runs an MCP tool.

The kit renders the same documents the portal, the admin interface, the WHMCS client area and the MCP clients receive. It does not run those clients. If your panel renders as you expect here, it renders the same everywhere, because every surface is a thin renderer of this document (see [declarative UI](../plugin-system.md#10-declarative-ui)).

### HTTP, VMs and audit

- `rackflow.http` records every request `ctx.http` makes: `rackflow.http.requests` (each with `method`, `url`, `host`, `headers`, `text`, `json()`), `rackflow.http.to("hooks.example.com")`. Answer with `rackflow.http.respond("example.com", 204, body)`, `respond_with(match, handler)` or `fail(match)`. The newest matching route wins; with no route the answer is `200` with an empty body. `rackflow.http.resolve("host", "10.0.0.5")` makes a host name resolve to an address, which is how you test that a private address is refused. The host's URL rules still apply: public addresses only, HTTPS only.
- `rackflow.vm` is the VM behind `ctx.guest` and `ctx.vm`: set `rackflow.vm.on_exec = lambda argv: {"exit_code": 0, "stdout": "ok"}`, `rackflow.vm.agent_ready = False` or `rackflow.vm.error = RuntimeError("down")`; read `rackflow.vm.commands` and `rackflow.vm.power_calls`.
- `rackflow.audit_entries("plugin.")` lists the audit rows whose action starts with the prefix: `action`, `resource_type`, `resource_id`, `metadata`.
- `rackflow.reset_rate_limits()` clears the action, tool and guest-command rate limits. An action allows 10 calls a minute per caller, so a test that clicks more often needs it.

## Testing a provider plugin

The kit supports plugins that offer a provider (API 1.1, see [kinds](README.md#15-plugin-kinds-api-11)), with no special fixture. `rackflow.install(PLUGIN)` runs the real install, including the permission check of every `provides` entry and the load that matches your `@plugin.provider` classes with the manifest. You then call the provider the way billing does, with the host's own `call_provider`, so the time limit, the two-call limit, the answer checks, the rate limit and the circuit breaker all run for real. Import the request and answer types from `app.plugin_host.kinds` in tests (inside the plugin they come from `rackflow_plugin.kinds`; both names are the same module once the kit has loaded a plugin).

```python
from pathlib import Path

import pytest

from app.plugin_host.testkit import assert_valid_package
from app.plugin_host.kinds import TaxIdCheckRequest
from app.plugin_host.providers import ProviderUnavailable, call_provider

PLUGIN = Path(__file__).resolve().parents[1]
REF = "norway_vat:registry"          # <plugin id>:<provider id>


def ask(tax_id="123456789"):
    request = TaxIdCheckRequest("NO", tax_id, "no_vat")
    return call_provider("tax_id_validator", REF, "check_online", request)


def test_package_is_valid():
    assert_valid_package(PLUGIN)


def test_a_known_number_is_valid_and_an_unknown_one_is_not(rackflow):
    rackflow.install(PLUGIN)
    rackflow.http.respond("registry.example.com", 200, "{}")
    assert ask().status == "valid"
    rackflow.http.respond("registry.example.com", 404, "")
    assert ask().status == "invalid"


def test_a_registry_outage_is_unavailable_not_invalid(rackflow):
    rackflow.install(PLUGIN)
    rackflow.http.respond("registry.example.com", 503, "")
    result = ask()
    assert (result.status, result.error_code) == ("unavailable", "HTTP_503")


def test_billing_is_refused_while_the_plugin_is_disabled(rackflow):
    plugin = rackflow.install(PLUGIN)
    plugin.disable()
    with pytest.raises(ProviderUnavailable) as refused:
        ask()
    assert refused.value.reason == "disabled"
```

What to test, and what the kit gives you for it:

- **Answers.** Call each method with a request built from the dataclasses and assert on the result. `call_provider` returns the *checked* answer, so a test that passes also proves the host accepts your answer. A wrong answer shows up as `ProviderUnavailable` with the reason `invalid_answer`; the plugin log (`plugin.log_messages("error")`) says which check failed.
- **Failures.** A method that raises gives the reason `error`; one that runs past its time limit gives `timeout` (pass `timeout_seconds=0.2` to `call_provider` to keep the test short; the floor is 0.1). Both write an error line to the plugin log, and `plugin.installation.consecutive_failures` counts them. Call it `health.FAILURES_TO_OPEN` times (from `app.plugin_host.health`) to see the circuit open, and `rackflow.audit_entries("plugin.provider_failed")` to see the audit entry.
- **Outbound HTTP.** `rackflow.http` answers `ctx.http` inside a provider exactly as it does for handlers (see above); the host's URL rules still apply.
- **Settings, store and state.** `plugin.configure(...)` and `plugin.state()` (the plugin-wide `ctx.store`) work as for any plugin.
- **Rate limits.** The kit counts them with the same fake counter as the other limits. Calls over the manifest's `rate_limit` give `rate_limited` without running your code, and `rackflow.reset_rate_limits()` resets the count.
- **Which providers exist.** `providers_of("tax_id_validator")` from `app.plugin_host.providers` lists the enabled providers of a kind whose circuit is closed and whose permission was granted.

The bundled [`vies_validator`](../../plugin_packages/vies_validator/tests/test_vies_plugin.py) and [`hmrc_exchange_rates`](../../plugin_packages/hmrc_exchange_rates/tests/test_hmrc_plugin.py) tests are larger examples. They live outside `testpaths`, so the VIES tests are also run from `tests/plugin_host/`; run your own with `pytest path/to/tests` as above.

## What the kit does not cover

- It has no helper for providers of its own. You call the host's `call_provider` and `providers_of` directly, and the types come from `app.plugin_host`, which the guide otherwise tells plugin code not to import: that is fine in a test, never in the plugin.
- It does not run billing. It checks that your provider gives answers the host accepts, not what billing does with them (for example how an `unavailable` tax ID check is charged).
- Provider calls run on real threads with real time limits, so a test that checks a time limit takes about as long as the limit; `rackflow.clock` does not speed it up.
- It does not run a browser, WHMCS or an MCP client. It checks the document they all render.
- It does not run MariaDB. A plugin reaches the database only through settings, state and logs, which the host manages, so the database engine is not part of what you test.
- It does not run the plugin worker as a process. `deliver()` and `run_schedule()` call the same functions the worker calls.

## Testing against another API version

Rackflow supports the previous major version of the API for a while after a new one ships (see the [deprecation policy](deprecation-policy.md)). Run your tests against both a checkout of the Rackflow release you target and `main`, and run `assert_valid_package` on each; warnings from the second tell you what is about to change.
