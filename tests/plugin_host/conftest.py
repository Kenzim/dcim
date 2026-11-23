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
    The report writer for abandoned handlers is waited for too: it writes on the same connection. Asking
    for ``monkeypatch`` keeps the test's patches (the test database among them) in place until both are done.
    """
    yield
    from app.plugin_host import invoke

    deadline = time.monotonic() + 10
    for _ in range(2):  # the handler threads, then the report writer they woke
        for thread in invoke._threads():
            thread.join(max(0.0, deadline - time.monotonic()))


@pytest.fixture
def plugin_env(db_session, mock_redis, monkeypatch, tmp_path):
    return setup_env(mock_redis, monkeypatch, tmp_path)
