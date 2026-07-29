"""Unit tests for the /docs, /redoc, /openapi.json env-gated exposure.

app.main is imported once per test session (with DISABLE_PUBLIC_API_DOCS=false
set in tests/conftest.py so other tests can introspect /openapi.json), so the
"disabled by default" behavior is verified here via a subprocess with a clean
environment instead of reloading the shared app.main module in-process.
"""
import json
import subprocess
import sys

_PROBE = """
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("INITIAL_ADMIN_USERNAME", "")
os.environ.setdefault("INITIAL_ADMIN_PASSWORD", "")
os.environ.setdefault("REQUIRE_SERVICE_INSTANCE_ENCRYPTION", "false")
import json
from app.main import app
print(json.dumps({"docs_url": app.docs_url, "redoc_url": app.redoc_url, "openapi_url": app.openapi_url}))
"""


def _probe(disable_public_api_docs: str = None) -> dict:
    # tests/conftest.py sets DISABLE_PUBLIC_API_DOCS=false in this process's
    # environment (for other tests that hit /openapi.json); build the child
    # environment explicitly so that override doesn't leak into these probes.
    import os as _os

    env = {k: v for k, v in _os.environ.items() if k != "DISABLE_PUBLIC_API_DOCS"}
    if disable_public_api_docs is not None:
        env["DISABLE_PUBLIC_API_DOCS"] = disable_public_api_docs
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_api_docs_disabled_by_default():
    urls = _probe()
    assert urls == {"docs_url": None, "redoc_url": None, "openapi_url": None}


def test_api_docs_enabled_when_explicitly_opted_in():
    urls = _probe(disable_public_api_docs="false")
    assert urls == {"docs_url": "/docs", "redoc_url": "/redoc", "openapi_url": "/openapi.json"}
