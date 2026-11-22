import time

import pytest

from tests.plugin_host.helpers import setup_env


@pytest.fixture(autouse=True)
def provider_calls_finish():
    """Let provider calls a test abandoned finish, so their slots do not make the next test busy."""
    yield
    from app.plugin_host import providers

    deadline = time.monotonic() + 10
    while providers._running and time.monotonic() < deadline:
        time.sleep(0.02)


@pytest.fixture
def plugin_env(db_session, mock_redis, monkeypatch, tmp_path):
    return setup_env(mock_redis, monkeypatch, tmp_path)
