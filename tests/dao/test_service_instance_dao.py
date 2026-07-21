import pytest
from cryptography.fernet import Fernet

from app.dao.service_instance_dao import ServiceInstanceDAO
from app.models.location import Location
from app.models.service_instance import ServiceInstance


def _create_location(db_session, name: str = "DC1") -> Location:
    location = Location(name=name, description="Test location")
    db_session.add(location)
    db_session.commit()
    db_session.refresh(location)
    return location


def test_create_update_and_verify_api_key(db_session):
    location = _create_location(db_session)

    row = ServiceInstanceDAO.create(
        db_session,
        location_id=location.id,
        service_type="dhcp",
        name="dhcp-runner",
        base_url="http://runner.local/",
        api_key="secret-key",
    )

    assert row.base_url == "http://runner.local"
    assert ServiceInstanceDAO.get_api_key(row) == "secret-key"
    assert ServiceInstanceDAO.verify_api_key(row, "secret-key") is True
    assert ServiceInstanceDAO.verify_api_key(row, "wrong") is False

    updated = ServiceInstanceDAO.update(
        db_session,
        row,
        name="dhcp-runner-2",
        base_url="http://runner2.local/",
        api_key="new-secret",
    )
    assert updated.name == "dhcp-runner-2"
    assert updated.base_url == "http://runner2.local"
    assert ServiceInstanceDAO.get_api_key(updated) == "new-secret"


def test_get_all_get_by_location_and_delete(db_session):
    location_a = _create_location(db_session, "LON-1")
    location_b = _create_location(db_session, "LON-2")

    first = ServiceInstanceDAO.create(
        db_session,
        location_id=location_a.id,
        service_type="dhcp",
        name="dhcp-a",
        base_url="http://a/",
        api_key="a",
    )
    ServiceInstanceDAO.create(
        db_session,
        location_id=location_b.id,
        service_type="tftp",
        name="tftp-b",
        base_url="http://b/",
        api_key="b",
    )

    all_rows = ServiceInstanceDAO.get_all(db_session)
    assert len(all_rows) == 2

    only_a = ServiceInstanceDAO.get_all(db_session, location_id=location_a.id)
    assert len(only_a) == 1
    assert only_a[0].name == "dhcp-a"

    by_loc_type = ServiceInstanceDAO.get_by_location_and_type(db_session, location_a.id, "dhcp")
    assert by_loc_type is not None
    assert by_loc_type.id == first.id

    updated = ServiceInstanceDAO.update_connection_test(db_session, by_loc_type, ok=True)
    assert updated.connection_ok is True
    assert updated.last_connection_test is not None

    assert ServiceInstanceDAO.delete(db_session, first.id) is True
    assert ServiceInstanceDAO.get_by_id(db_session, first.id) is None
    assert ServiceInstanceDAO.delete(db_session, 99999) is False


def test_api_key_encrypted_at_rest_when_key_configured(db_session, monkeypatch):
    """With an encryption key configured, the stored value is ciphertext but
    get_api_key/verify still return/compare the plaintext."""
    import app.core.service_instance_crypto as crypto
    monkeypatch.setattr(crypto.settings, "service_instance_encryption_key", Fernet.generate_key().decode())

    location = _create_location(db_session, "ENC-DC")
    row = ServiceInstanceDAO.create(
        db_session,
        location_id=location.id,
        service_type="dhcp",
        name="dhcp-enc",
        base_url="http://runner.local/",
        api_key="super-secret",
    )

    # Raw column must not contain the plaintext.
    assert "super-secret" not in (row.api_key_encrypted or "")
    assert row.api_key_encrypted.startswith("gAAAAA")  # Fernet token prefix
    # But the DAO round-trips the plaintext.
    assert ServiceInstanceDAO.get_api_key(row) == "super-secret"
    assert ServiceInstanceDAO.verify_api_key(row, "super-secret") is True
    assert ServiceInstanceDAO.verify_api_key(row, "nope") is False


def test_legacy_plaintext_key_reencrypted_on_verify(db_session, monkeypatch):
    """A legacy plaintext value is re-encrypted the next time it verifies."""
    location = _create_location(db_session, "LEG-DC")
    # Insert a row with a plaintext key directly (simulates legacy data).
    row = ServiceInstance(
        location_id=location.id,
        service_type="tftp",
        name="legacy",
        base_url="http://legacy.local",
        api_key_encrypted="legacy-plain",
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)

    # Now enable encryption and verify.
    import app.core.service_instance_crypto as crypto
    monkeypatch.setattr(crypto.settings, "service_instance_encryption_key", Fernet.generate_key().decode())

    assert ServiceInstanceDAO.get_api_key(row) == "legacy-plain"
    assert ServiceInstanceDAO.verify_api_key(row, "legacy-plain", db=db_session) is True
    db_session.refresh(row)
    # Stored value should now be encrypted.
    assert row.api_key_encrypted.startswith("gAAAAA")
    assert ServiceInstanceDAO.get_api_key(row) == "legacy-plain"

