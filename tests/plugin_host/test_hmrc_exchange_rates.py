"""Runs the HMRC exchange rate plugin's own tests from the default test paths, which do not include plugin_packages/."""

from app.plugin_host.testkit.fixtures import rackflow  # noqa: F401
from plugin_packages.hmrc_exchange_rates.tests.test_hmrc_plugin import *  # noqa: F401,F403
