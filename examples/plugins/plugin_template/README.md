# Plugin template

A working starting point for a Rackflow plugin. It has one setting, one event handler, one panel on the service page and one action, and a test for each. Copy it, rename it, and replace the pieces.

## Use it

1. Copy this folder and give it your plugin's id as its name. The id uses lowercase letters, digits and underscores, starts with a letter and is 3 to 48 characters long: `cp -r plugin_template my_plugin`.
2. In `plugin.json` set `id` to the same name, and set `name`, `description`, `author`, `licence` and `version`.
3. In `plugin.py` replace the handlers. Each handler you register must be declared in `plugin.json` (events, hooks, schedules, actions, MCP tools), and each declared one must be registered.
4. In `tests/test_plugin_template.py` rename the file (test file names must be unique when several plugins are tested together) and write tests for your handlers.
5. Run the tests from a Rackflow checkout: `PYTHONPATH=/path/to/rackflow pytest my_plugin/tests`.

## What is here

| File | What it is |
|---|---|
| `plugin.json` | The manifest: id, version, API range, permissions, settings, events, actions and the panel. |
| `plugin.py` | The entry module. `register(plugin)` binds handlers to the names in the manifest. |
| `tests/conftest.py` | Makes the `rackflow` test fixture available. |
| `tests/test_plugin_template.py` | A package check and tests that install the plugin and fire an event and an action. |

See the [plugin author guide](../../../docs/plugins/README.md), the [testing guide](../../../docs/plugins/testing.md) and the [API reference](../../../docs/plugins/api-reference.md).
