"""Compatibility test kit for Rackflow plugins (docs/plugins/testing.md).

Import this package before any other ``app`` module: it gives the settings a throwaway
in-memory database, so a plugin's tests need neither a database server nor a ``.env`` file.
"""

import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("INITIAL_ADMIN_USERNAME", "")
os.environ.setdefault("INITIAL_ADMIN_PASSWORD", "")
os.environ.setdefault("MCP_ENABLED", "false")
os.environ.setdefault("CLIENT_MCP_ENABLED", "false")
os.environ.setdefault("RUN_PLUGIN_WORKER", "false")
os.environ.setdefault("RUN_ALERT_WORKER", "false")

from app.plugin_host.testkit.check import PackageReport, assert_valid_package, check_package  # noqa: E402
from app.plugin_host.testkit.fakes import FakeHttp, FakeVm, HttpRequest  # noqa: E402
from app.plugin_host.testkit.harness import (  # noqa: E402
    Clock,
    DeliveryInfo,
    Fired,
    Harness,
    InstalledPlugin,
    ScheduleRun,
)
from app.plugin_host.testkit.samples import sample_payload  # noqa: E402

__all__ = [
    "Clock",
    "DeliveryInfo",
    "FakeHttp",
    "FakeVm",
    "Fired",
    "Harness",
    "HttpRequest",
    "InstalledPlugin",
    "PackageReport",
    "ScheduleRun",
    "assert_valid_package",
    "check_package",
    "sample_payload",
]
