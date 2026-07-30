import pytest
from unittest.mock import MagicMock, patch
from app.services import tftp_config_service as mod


def row(**overrides):
    values = dict(enabled=True, root_directory="/srv/tftp", bind_address="0.0.0.0",
                  bind_port=69, allow_create=True, verbose=True, ipv4_only=True)
    values.update(overrides)
    return MagicMock(**values)


def test_default_root_directory(monkeypatch):
    monkeypatch.delenv("TFTP_ROOT_DIRECTORY", raising=False)
    assert mod._default_root_directory() == "/shared/tftp"
    monkeypatch.setenv("TFTP_ROOT_DIRECTORY", "/env")
    assert mod._default_root_directory() == "/env"


def test_row_to_config():
    config = mod._row_to_config(row(bind_port=1069, verbose=False))
    assert config.bind_port == 1069 and not config.verbose


@pytest.mark.parametrize("existing", [True, False])
def test_get_config_reads_or_creates(monkeypatch, existing):
    existing_row, created = row(), row(root_directory="/created")
    monkeypatch.setattr(mod.TFTPConfigDAO, "get_config", lambda db: existing_row if existing else None)
    monkeypatch.setattr(mod.TFTPConfigDAO, "get_or_create", lambda *args: created)
    config = mod.TFTPConfigService().get_config(MagicMock())
    assert config.root_directory == ("/srv/tftp" if existing else "/created")


@pytest.mark.parametrize("kwargs", [
    {"enabled": False}, {"bind_port": 1069}, {"root_directory": "/new"},
    {"allow_create": False, "verbose": False, "ipv4_only": False},
])
def test_update_config_preserves_unspecified_fields(monkeypatch, kwargs):
    db, stored = MagicMock(), row()
    monkeypatch.setattr(mod.TFTPConfigDAO, "get_config", lambda _: stored)
    update = MagicMock(); monkeypatch.setattr(mod.TFTPConfigDAO, "update", update)
    result = mod.TFTPConfigService().update_config(db, **kwargs, ignored=None)
    assert update.call_args.args == (db, stored)
    for key, value in kwargs.items():
        assert getattr(result, key) == value
    assert result.bind_address == "0.0.0.0"


def test_update_config_creates_missing_row(monkeypatch):
    created, db = row(), MagicMock()
    monkeypatch.setattr(mod.TFTPConfigDAO, "get_config", lambda _: None)
    get_or_create = MagicMock(return_value=created)
    monkeypatch.setattr(mod.TFTPConfigDAO, "get_or_create", get_or_create)
    monkeypatch.setattr(mod.TFTPConfigDAO, "update", MagicMock())
    assert mod.TFTPConfigService().update_config(db, bind_address="127.0.0.1").bind_address == "127.0.0.1"
    get_or_create.assert_called_once()


def test_reload_delegates_to_get(monkeypatch):
    service = mod.TFTPConfigService(); service.get_config = MagicMock(return_value="config")
    assert service.reload(MagicMock()) == "config"


def test_global_service_is_cached(monkeypatch):
    monkeypatch.setattr(mod, "_tftp_config_service", None)
    assert mod.get_tftp_config_service() is mod.get_tftp_config_service()
