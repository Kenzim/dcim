import pytest

from tests.plugin_host.helpers import setup_env


@pytest.fixture
def plugin_env(db_session, mock_redis, monkeypatch, tmp_path):
    return setup_env(mock_redis, monkeypatch, tmp_path)
