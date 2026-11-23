# Plugin API changelog

Each entry lists what a plugin author needs to know for one API version: what was added, deprecated, removed or fixed. See the [deprecation policy](deprecation-policy.md) for what each kind of change means.

## API 1.1

A minor version. It only adds: every plugin written for 1.0 loads and behaves as before, with no change to its manifest or code. The generated [API reference](api-reference.md) is for 1.1, and [`api-surface-1.1.json`](api-surface-1.1.json) is its frozen surface; [`api-surface-1.0.json`](api-surface-1.0.json) is unchanged.

Added:

- **Provider kinds.** A plugin can offer typed providers that Rackflow's billing calls directly. The manifest has a new optional key, `provides` (at most ten entries; each has `kind`, `id`, `label`, `description`, `countries`, `id_types`, `currencies`, `timeout_seconds`, `rate_limit` and `pack`), and `register` has a new decorator, `@plugin.provider(kind, id)`. Four kinds exist: `tax_id_validator`, `tax_rule_pack`, `tax_provider` and `currency_rates`.
- **The `rackflow_plugin.kinds` module.** The frozen dataclasses and protocols of the four kinds, and `current_context()`, which gives a provider method the usual `ctx` (with `ctx.kind == "provider"`).
- **Four permissions**, one for each kind: `tax.validate_id` (medium risk), `tax.rule_pack` (medium), `tax.calculate` (high) and `currency.rates` (medium). The manifest is refused when a `provides` entry lacks the permission of its kind.
- **Four events**, all needing `billing.read`: `tax_id.checked` (never carries the number), `invoice.reminder_sent`, `service.termination_scheduled` and `service.cancellation_requested`. The catalogue now has 22 events. The description of `billing.read` was widened to name them.
- **Failure isolation for providers.** Each provider call has a time limit (`timeout_seconds`, 8 s by default, 1 to 30), runs on its own thread, and a plugin may have two calls running at once; a third is refused as `busy`. Failures feed the same circuit breaker as other plugin code, and a failure that opens the circuit writes the audit entry `plugin.provider_failed`. Answers are checked by exact type and against billing's own request, and an invalid answer is a failure. See [Calls to providers](api-reference.md#calls-to-providers) in the reference.
- **Bundled plugins.** Rackflow reads first-party packages from a second, read-only folder, `settings.bundled_plugins_dir` (default `plugin_packages/`). They show as bundled on the admin plugin page and use the ordinary lifecycle; nothing is installed or enabled automatically. The first two are `vies_validator` (EU VAT number checks) and `hmrc_exchange_rates` (monthly HMRC rates).
- **The `effective_api` of an installation:** the newest supported API version that the plugin's `rackflow_api` range allows, recorded at install and upgrade and returned by the admin plugin API. See the [deprecation policy](deprecation-policy.md#effective_api-what-version-a-plugin-runs-as).

Things to know when you read an event or the SDK on 1.1:

- `ctx.api_version` and the `version` of every event envelope now say `"1.1"`, for every plugin, whatever its `rackflow_api` range. They name the running Rackflow's API version. Do not compare them for equality.
- The folder names and manifest ids of the bundled plugins (`vies_validator` and `hmrc_exchange_rates` today, and any Rackflow adds later) are reserved. A plugin in the plugins folder that uses one is refused, with the error "`<name>` is a reserved name used by a plugin Rackflow ships; rename this plugin.", both when an administrator installs or upgrades it and when Rackflow loads it. The bundled plugin is not affected and keeps loading and running.
- A manifest with `provides` is refused when its `rackflow_api` range allows only 1.0 (for example `==1.0`). The other additions are available to a plugin whatever its range.

Not in 1.1: plugin-owned tables, placements other than the service page, and the kinds the design reserves for later (accounting export, notification channels, import sources, fraud checks and hardware adapters).

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
