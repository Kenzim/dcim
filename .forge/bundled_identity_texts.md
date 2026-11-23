# Proposed CODEBASE.md rows (bundled identity)

| Path | Row text |
|---|---|
| `alembic/versions/plugin_bundled_identity.py` | Adds `plugin_installations.bundled` and backfills it by where the directory exists now (bundled root only: true; plugins folder only or neither: false; both: true only when the stored hash equals the bundled copy's). Never decided by a hash mismatch alone. |
| `app/plugin_host/loader.py` | `package_origin(directory)` says which root `find_package` takes a package from; `root_clash_reason(...)` gives the refusal text when that root differs from the installation's `bundled` flag. |
| `app/plugin_host/lifecycle.py` | `install` records `bundled`, and refuses a bundled install while rows of an earlier plugin of that name are kept (unless the last uninstall of that id was a bundled one). `purge_data` is the explicit, audited way to delete them. `upgrade`, `set_enabled`, `_upgrade_view` and `summary` (`upgrade_blocked`) refuse a package from the other root. |
| `app/plugin_host/runtime.py` | `load_installed` returns None and sets `files_changed` with the clash reason when the package found is from the other root than the installation's. |
| `app/api/plugin_host.py` | `POST /api/admin/plugins/{plugin_id}/purge-data` deletes the data an uninstall kept, for a plugin that is no longer installed; audited as `plugin.purge_data`. |
| `tests/plugin_host/test_bundled_identity.py` | Third-party install versus a later bundled plugin of the same name, kept data and purge, the other direction, the backfill. |
