"""The route list in `whmcs_routes.py` is the contract's index: it must match both the
FastAPI application and the PHP module that calls it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.main import app

from tests.contract.whmcs_routes import BY_KEY, ROUTES, SERVED_BUT_NOT_CALLED

PHP_DIR = Path(__file__).resolve().parents[2] / "whmcs" / "modules" / "servers" / "rackflow"
SERVICE_BASE = "/billing/services/{service_id}"


def _app_routes() -> set[tuple[str, str]]:
    out = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not path or not methods or not path.startswith("/api/billing/"):
            continue
        for method in methods:
            out.add((method, path[len("/api"):]))
    return out


def test_route_keys_and_pairs_are_unique():
    assert len(BY_KEY) == len(ROUTES)
    pairs = [(r.method, r.path) for r in ROUTES]
    assert len(set(pairs)) == len(pairs)


@pytest.mark.parametrize("route", ROUTES, ids=lambda r: r.key)
def test_listed_route_exists_in_the_app(route):
    assert (route.method, route.path) in _app_routes(), (
        f"{route.method} /api{route.path} is in whmcs_routes.py but no longer served by the application"
    )


@pytest.mark.parametrize("method,path", SERVED_BUT_NOT_CALLED)
def test_not_called_routes_still_exist(method, path):
    assert (method, path) in _app_routes()


def test_every_php_call_is_listed():
    """A new `/api/billing/...` call in the PHP module must be added to the route list."""
    if not PHP_DIR.is_dir():
        pytest.skip("WHMCS module is not in this checkout")
    source = "\n".join(p.read_text(encoding="utf-8") for p in sorted(PHP_DIR.glob("*.php")))
    listed = {(r.method, r.path) for r in ROUTES}
    listed_paths = {path for _, path in listed}

    # Full literals such as '/api/billing/server-groups' or the start of a path.
    for literal in sorted(set(re.findall(r"'/api(/billing/[^'?]*)", source))):
        if literal.endswith("/"):
            assert any(p.startswith(literal) for p in listed_paths), f"PHP calls {literal}... which is not listed"
        else:
            assert literal in listed_paths, f"PHP calls /api{literal} which is not listed"

    # Suffixes appended to a service id: `$id . '/suspend'`.
    tail = r"'(/[a-z][a-z0-9\-]*(?:/[a-z][a-z0-9\-]*)*/?)"
    suffixes = set(re.findall(r"\$\w*(?:Id|basePath)\s*\.\s*(?!'/modules)" + tail, source))
    # Suffixes handed to the backup and slot helpers.
    for helper in ("backupApiAction", "slotFetch"):
        suffixes |= {"/" + s for s in re.findall(rf"rackflow_{helper}\(\$params, '([^']+)'", source)}
    suffixes |= {"/backup-slot/" + s for s in re.findall(r"rackflow_slotPost\(\$params, [^,]+, '([a-z]+)'", source)}
    assert suffixes, "no service sub-paths found in the PHP module; the extraction pattern is stale"
    for suffix in sorted(suffixes):
        want = SERVICE_BASE + suffix
        if suffix.endswith("/"):
            ok = any(p.startswith(want) for p in listed_paths)
        else:
            ok = want in listed_paths
        assert ok, f"PHP calls /api/billing/services/{{id}}{suffix} which is not listed"


def test_strategy_action_names_the_module_runs():
    """The module runs these strategy actions through `run_action`; they are pinned in test_vm_operations."""
    if not PHP_DIR.is_dir():
        pytest.skip("WHMCS module is not in this checkout")
    source = (PHP_DIR / "rackflow.php").read_text(encoding="utf-8")
    names = set(re.findall(r"rackflow_runStrategyAction\(\$params, '([a-z_]+)'", source))
    assert names == {"change_password", "reset_network", "randomize_smbios"}


def test_every_route_has_a_behaviour_test():
    """Each route key must be used by a shape test, not only by the parametrised auth checks."""
    here = Path(__file__).parent
    skip = {"test_route_list.py", "test_auth_and_errors.py"}
    source = "\n".join(p.read_text(encoding="utf-8") for p in sorted(here.glob("test_*.py")) if p.name not in skip)
    untested = [r.key for r in ROUTES if f'"{r.key}"' not in source]
    assert not untested, f"routes with no shape test: {untested}"
