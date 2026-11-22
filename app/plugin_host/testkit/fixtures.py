"""pytest fixtures of the plugin test kit.

In a plugin's ``conftest.py``::

    from app.plugin_host.testkit.fixtures import rackflow  # noqa: F401
"""

from __future__ import annotations

import pytest

from app.plugin_host.testkit.harness import Harness


@pytest.fixture
def rackflow(monkeypatch, tmp_path):
    """A fresh in-memory Rackflow plugin host for one test."""
    harness = Harness(monkeypatch, tmp_path)
    try:
        yield harness
    finally:
        harness.close()
