"""ISO catalog validation helpers and location-aware library."""
import pytest

from app.dao.location_dao import LocationDAO
from app.dao.runner_dao import RunnerDAO
from app.services.virtual_media.base import VirtualMediaUnavailable
from app.services.virtual_media.iso_catalog import list_iso_files_for_location, validate_iso_filename


def test_validate_iso_filename_accepts_basename():
    assert validate_iso_filename("rescue.iso") == "rescue.iso"


@pytest.mark.parametrize("name", ["../x.iso", "a/b.iso", "", "readme.txt"])
def test_validate_iso_filename_rejects_invalid(name):
    with pytest.raises(VirtualMediaUnavailable, match="Invalid ISO filename"):
        validate_iso_filename(name)


def test_list_iso_files_for_location_falls_back_to_central(db_session, tmp_path, monkeypatch):
    iso = tmp_path / "rescue.iso"
    iso.write_bytes(b"iso")
    monkeypatch.setattr("app.services.virtual_media.iso_catalog._ISOS_DIR", tmp_path)
    loc = LocationDAO.create(db_session, name="loc-iso-fallback")
    rows = list_iso_files_for_location(db_session, loc.id)
    assert rows[0]["filename"] == "rescue.iso"


def test_list_iso_files_for_location_uses_runner_cache(db_session, mock_redis, monkeypatch):
    monkeypatch.setattr("app.core.redis.redis_client", mock_redis)
    monkeypatch.setattr("app.services.runners.cache.redis_mod.redis_client", mock_redis)
    loc = LocationDAO.create(db_session, name="loc-iso-runner")
    row, _key = RunnerDAO.create(db_session, "media-1", location_id=loc.id, capabilities=["media"])
    row.state = {"isos": [{"filename": "local.iso", "size_bytes": 10, "size_mb": 0.0}]}
    db_session.commit()
    rows = list_iso_files_for_location(db_session, loc.id)
    assert rows == [{"filename": "local.iso", "size_bytes": 10, "size_mb": 0.0}]
