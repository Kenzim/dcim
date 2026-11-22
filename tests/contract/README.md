# WHMCS contract tests

These tests pin the request and response shapes of every `/api/billing/*` route that the WHMCS module in `whmcs/modules/servers/rackflow/` calls. Their purpose is that a later change to billing behaviour that would break Kenzi's production WHMCS fails here first, with the route named in the failure.

The tests are black-box: they send the requests the PHP module sends (Bearer billing integration key, JSON bodies encoded like PHP `json_encode`) and assert status codes, required keys and their JSON types, and the `detail` string that the module shows to users on errors. They do not change `app/api/billing.py` or any WHMCS file.

## Files

| File | What it holds |
|---|---|
| `whmcs_routes.py` | The route list as data (`ROUTES`): method, path, callers in the PHP, query and body fields sent, response fields read. |
| `test_route_list.py` | Fails if a listed route no longer exists in the FastAPI app, if the PHP calls a path that is not listed, or if a listed route has no shape test. |
| `test_auth_and_errors.py` | For every route: 401 without or with a wrong key, 404 `Service not found` for an unknown id or another integration's service. |
| `test_catalogue_and_linking.py` | Connection test, server groups, clusters, products, ISOs, server lookup, register, create, adopt, link, unlink, lookup, placement. |
| `test_lifecycle_status.py` | Suspend, unsuspend, terminate, power (including the plugin before-hook 409), status, debug. |
| `test_operations.py` | IP pool, reinstall, strategy actions, backups, backup slots, storage, proxies. Includes the known mismatch described below. |
| `test_consoles_plugins.py` | Console tickets, portal sign-in, virtual media, plugin panels and actions, add-on status. |
| `shapes.py`, `service_shapes.py` | Small helpers (`assert_shape`, `assert_error`) and the shared response shapes. |
| `conftest.py` | The `whmcs` fixture: an integration key, service builders and a `call(route_key, ...)` helper. |

## How the route list was derived

1. Every `rackflow_apiCall(...)` call and every helper that wraps it in the PHP files of the module (`rackflow.php`, `hooks.php`, `admin_link.php`, `*_open.php`, `*_action.php`, `plugin_action.php`) was read. Paths are either full literals (`'/api/billing/server-groups'`) or a service id followed by a suffix (`'/api/billing/services/' . $id . '/suspend'`).
2. For each call the method, query string, JSON body keys and the response keys read afterwards (`$result['data'][...]`) were noted in `whmcs_routes.py`.
3. `test_route_list.py::test_every_php_call_is_listed` repeats the path extraction mechanically with regular expressions over the PHP source, so a new call in the module that is not in the list fails the test. The extraction is a safety net, not a parser: when it reports a path you know is wrong, read the PHP and adjust the list or the pattern.

The module currently calls 54 routes. Three of them (`plugin-ui`, `plugin-actions` and the `addons` status route) are served by `app/api/plugin_host.py`, not `app/api/billing.py`, under the same `/api/billing` prefix.

## Updating after an intentional change

If a change is meant to alter what WHMCS sees:

1. Change the module and the API together and note it in the release notes, because a deployed WHMCS keeps running the old PHP until it is replaced.
2. Update the affected `Route` entry in `whmcs_routes.py` and the matching test. Keep the test strict: add the new key to the shape rather than loosening a type.
3. Run `python -m pytest tests/contract -q`.

If the PHP gains a call, add a `Route` and a test that uses its key (`whmcs.call("key", ...)`). `test_every_route_has_a_behaviour_test` fails until a shape test exists.

## Empty PHP arrays

PHP `json_encode(array())` writes `[]`. Current module code sends `{}` for an empty body and casts the `params` and `service_config` fields to objects. Older copies of the module still send `[]`, so the API accepts an empty list in place of an empty object for the strategy action `params`, the `service_config` of both create routes, `template_parameters` of reinstall-os, and the whole body of VM reinstall and Windows password reset (`app/schemas/empty_object.py`). A non-empty list is still refused with 422. The tests `test_run_action_accepts_empty_params_as_object_or_list`, `test_vm_reinstall_accepts_an_empty_list_body_like_an_empty_object`, `test_windows_password_reset_accepts_an_empty_body_as_object_or_list` and `test_create_accepts_an_empty_service_config_list_like_an_empty_object` pin both spellings.
