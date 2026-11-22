# Plugin API deprecation policy

This is the promise Rackflow makes to plugin authors about changing the plugin API, and the way authors find out about a change in time. It applies to everything in the [API reference](api-reference.md): the manifest, the event catalogue and payloads, the permissions, the SDK context, the handler signatures and the render documents. The contract itself is in [the plugin system design](../plugin-system.md), section 3.3.

## Versions

- The plugin API version is `MAJOR.MINOR`, held in `API_VERSION` (`app/plugin_host/catalogue.py`). It is shown to admins at the top of the plugin page and returned by `GET /api/admin/plugins`. Plugins read it as `ctx.api_version`, and every event envelope carries it in `version`.
- A plugin states which versions it was written for in `plugin.json`, in `rackflow_api`: a comma-separated list of comparisons joined by "and", such as `>=1.0,<2.0`. The operators are `>=`, `>`, `<=`, `<` and `==`; the version numbers have one to three parts, such as `1`, `1.0` or `1.2.3`. Write the lower bound as the oldest API version that has everything the plugin uses, and the upper bound as the next major version. A manifest without an upper bound claims to work on every future major version, which no author can promise. The test kit warns about it.
- The plugin's own `version` is separate. It is `MAJOR.MINOR.PATCH`, set by the author, and decides what an upgrade may do: Rackflow refuses downgrades, and asks the admin to accept again any permission an upgrade adds.

## Where the version is checked

| When | What Rackflow does if the range does not include the running API version |
|---|---|
| The admin lists available plugins | The package is shown as invalid with the message "needs Rackflow plugin API ...". |
| Install and upgrade | Refused with the same message. |
| A plugin's files are loaded (after a restart or an upgrade of Rackflow) | The plugin cannot be loaded; it is not run and the admin sees the same message. |
| In the author's tests | `check_package(path).supports("2.0")` tells the author whether the range admits a version, and `assert_valid_package` fails on a range that excludes the current version. |

The design document also describes an admin warning for plugins whose range excludes the next major version before it ships. That warning is not built yet; until it is, authors learn about a major version from the changelog (below) and the notice period in this policy.

## What counts as a breaking change

A change is breaking when a plugin that is valid and correct on version X.Y can fail, or behave differently in a way it did not choose, on a later version of the same major version. Breaking changes happen only in a major version. These are breaking:

- removing or renaming an event, a permission, a manifest key, an SDK member, a decorator or a lifecycle step;
- removing, renaming or retyping a payload field of an event, or sending a field that used to be present as null;
- making a manifest key required that was optional, or narrowing what a key accepts (a shorter limit, a stricter pattern, a removed option);
- changing what an event means, or when it fires, so that a handler's assumptions fail (for example firing `invoice.paid` for a different set of invoices);
- changing a handler's arguments, return value or the exceptions it must handle;
- requiring a permission for something a permission-free call could do before;
- lowering a published limit (time limits, output sizes, rate limits) below the documented value;
- changing the shape of a render document or action result that a surface depends on.

These are not breaking, and may arrive in a minor version:

- a new event, permission, manifest key (optional), SDK member, element type, lifecycle step or handler kind;
- a new optional payload field. Handlers must ignore fields they do not know, and must not assume the set of keys is fixed;
- a new value in a field that is documented as open, such as a new `service.power_requested` source;
- more generous limits, faster or more reliable delivery, clearer messages and better admin screens;
- bug fixes that make the host do what the documentation already said.

Plugin kinds (the `provides` section of the design document) will arrive as an additive minor version: a new optional manifest key, new permissions and new registration decorators. They change nothing for a plugin that does not use them.

The reserved key `tables` is refused in API 1.0. Accepting it later is an addition.

## How the promise is enforced

`docs/plugins/api-surface-<version>.json` is a frozen list of everything a plugin could depend on in that API version: the permissions, the events with their fields, the manifest keys, the SDK members, the lifecycle steps, the error classes. `tests/plugin_host/test_reference.py` compares every snapshot of the current major version with the code, and fails the test run if anything was removed, if an event lost a field or changed permission, or if a manifest model gained a required key. A snapshot is written when an API version is released and never edited afterwards.

The same test run checks that [the reference](api-reference.md) is exactly what the code generates, so a new event, permission or SDK member cannot ship without being described.

## Deprecation

When something should go away, it goes in two steps.

1. **Deprecate in a minor version.** The item keeps working exactly as before. It is added to `DEPRECATIONS` in `app/plugin_host/testkit/deprecations.py` with the version it is deprecated since, the major version that removes it, and what to use instead. It appears in the "Deprecated items" section of the API reference and in the changelog.
2. **Remove in a major version.** Removal is never earlier than the next major version after the one that deprecated the item. Rackflow keeps accepting plugins written for the previous major version for at least one Rackflow release after the new major version ships, and for no fewer than 90 days. Plugins written for the old major version keep their `rackflow_api` range and keep running during that time. After it, the old range is refused as in the table above.

An item is never deprecated in the same minor version that introduces its replacement unless the replacement is complete, and the replacement is always available for at least one minor version before the old item is removed in a major.

Security fixes are the exception. When an item cannot be kept without exposing admins or clients, Rackflow may remove or restrict it in a minor version. The change is named in the changelog as a security change, and it is limited to what the problem needs.

## How authors are warned

- **The changelog.** [`CHANGELOG.md`](CHANGELOG.md) lists, for each API version, what was added, deprecated, removed and fixed. Read it when you upgrade Rackflow or when you raise `rackflow_api`.
- **The test kit.** `check_package(path)` and `assert_valid_package(path)` (see [testing](testing.md)) add a warning for every deprecated permission, event, hook, manifest key or SDK member that the package uses, with the version that removes it and what to use instead. Run them in the plugin's test suite; treat a warning as a to-do.
- **The reference.** The "Deprecated items" section of the [API reference](api-reference.md) lists every deprecated item. It is generated from the same registry, so it cannot disagree with the warnings.
- **The admin.** The plugin page shows the plugin API version and flags a plugin that is invalid for it. There is no runtime warning to admins for a deprecated item in API 1.0.

## What authors should do

1. Declare `rackflow_api` as `>=X.Y,<NEXT_MAJOR.0`, with X.Y the oldest version that has what you use.
2. Add `assert_valid_package` to your tests and fail the build on its errors. Read its warnings when you update.
3. Do not depend on anything the reference does not list: private modules under `app/`, the database, files outside your package, the timing of delivery, or the order of keys in a payload.
4. Ignore payload fields you do not know.
5. Before a new major version ships, run your tests against its release candidate, raise the upper bound if they pass, and publish a new plugin version. Admins upgrade through the normal plugin upgrade, which shows them any permission you added.
