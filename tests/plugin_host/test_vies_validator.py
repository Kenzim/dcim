"""Runs the VIES validator's own tests from the default test paths, which do not include plugin_packages/."""

from app.plugin_host.testkit.fixtures import rackflow  # noqa: F401
from plugin_packages.vies_validator.tests.test_vies_plugin import *  # noqa: F401,F403
