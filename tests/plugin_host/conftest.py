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


@pytest.fixture(autouse=True)
def plugin_invocations_finish(monkeypatch):
    """Let handlers a test abandoned (a before hook that timed out) finish before the next test starts.

    The handler's thread commits or rolls back and closes its session on the shared test connection when
    it returns, which would otherwise roll back whatever uncommitted rows the next test on this worker has.
    """
    from concurrent.futures import wait

    from app.plugin_host import invoke

    submitted = []
    submit = invoke._executor.submit

    def recording_submit(*args, **kwargs):
        future = submit(*args, **kwargs)
        submitted.append(future)
        return future

    monkeypatch.setattr(invoke._executor, "submit", recording_submit)
    yield
    wait(submitted, timeout=10)


@pytest.fixture
def plugin_env(db_session, mock_redis, monkeypatch, tmp_path):
    return setup_env(mock_redis, monkeypatch, tmp_path)
