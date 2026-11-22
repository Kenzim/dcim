# Plugin API changelog

Each entry lists what a plugin author needs to know for one API version: what was added, deprecated, removed or fixed. See the [deprecation policy](deprecation-policy.md) for what each kind of change means.

## API 1.0

The first version of the plugin API. The contract is [the plugin system design](../plugin-system.md); the generated [API reference](api-reference.md) lists everything in it.

- Package format, `plugin.json` manifest and the entry module with `register(plugin)`.
- Lifecycle: install, enable, disable, upgrade, uninstall, with permission approval by an admin.
- Settings with encrypted secrets, plugin state, plugin log and audit entries.
- Event bus: 18 events, after handlers with retries, before hooks on `service.power_requested`.
- Schedules, global or per service, and per-service enablement with add-on links.
- Declarative panels and actions for the admin interface, client portal and WHMCS; MCP tools for staff and clients.
- SDK context: services, VM power and guest commands, client notices, outbound HTTP.
- Failure isolation: time limits, circuit breaker, automatic disable.

Not in 1.0: plugin kinds (typed providers such as tax validators, currency rates or notification channels), plugin-owned tables, placements other than the service page.
