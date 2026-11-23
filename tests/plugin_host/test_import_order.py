"""The plugin host must import on its own, before anything has loaded ``app.models``.

A plugin author's test run imports ``app.plugin_host.testkit`` first (see
``examples/plugins/conftest.py``). The full suite cannot show an import cycle
there, because ``tests/conftest.py`` imports ``app.models`` before any test
module, so each import runs in a fresh interpreter.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "module",
    ["app.plugin_host.testkit", "app.plugin_host.loader", "app.plugin_host.runtime"],
)
def test_plugin_host_module_imports_first(module: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:]
