from app.dao.tftp_config_dao import TFTPConfigDAO


def test_tftp_config_is_absent_before_creation(db_session):
    assert TFTPConfigDAO.get_config(db_session) is None


def test_tftp_config_get_or_create_persists_documented_defaults(db_session):
    config = TFTPConfigDAO.get_or_create(db_session, "/srv/tftp")
    assert config.id == TFTPConfigDAO.CONFIG_ID
    assert config.root_directory == "/srv/tftp"
    assert config.enabled is True
    assert config.bind_address == "0.0.0.0"
    assert config.bind_port == 69
    assert config.allow_create is True
    assert config.verbose is True
    assert config.ipv4_only is True


def test_tftp_config_get_or_create_reuses_existing_row_and_update_commits(db_session):
    first = TFTPConfigDAO.get_or_create(db_session, "/srv/first")
    second = TFTPConfigDAO.get_or_create(db_session, "/srv/ignored")
    assert second.id == first.id
    assert second.root_directory == "/srv/first"
    second.enabled = False
    second.bind_port = 1069
    updated = TFTPConfigDAO.update(db_session, second)
    assert updated.enabled is False
    assert TFTPConfigDAO.get_config(db_session).bind_port == 1069
