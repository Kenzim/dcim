"""SuperMicro X9 CIFS virtual-media helpers."""
from app.services.virtual_media.base import VirtualMediaUnavailable
from app.services.virtual_media.registry import get_profile, list_profiles
from app.services.virtual_media.supermicro_x9 import (
    SuperMicroX9VirtualMediaProfile,
    parse_cifs_share,
    parse_vmstatus_xml,
    unc_iso_path,
)
import pytest


def test_registry_lists_supermicro_x9():
    ids = {row["id"] for row in list_profiles()}
    assert "supermicro_x9" in ids
    assert isinstance(get_profile("supermicro_x9"), SuperMicroX9VirtualMediaProfile)


def test_parse_vmstatus_cd_empty_and_inserted():
    empty = """<?xml version="1.0" ?>
<VM>
<CODE NO="1"/>
<DEVICE ID="0" STATUS="255"/>
<DEVICE ID="1" STATUS="255"/>
</VM>"""
    status = parse_vmstatus_xml(empty)
    assert status.inserted is False
    assert status.device == "CD"

    mounted = empty.replace('ID="1" STATUS="255"', 'ID="1" STATUS="1"')
    assert parse_vmstatus_xml(mounted).inserted is True


def test_unc_iso_path_normalizes_slashes():
    assert unc_iso_path("/iso/ubuntu.iso") == "\\iso\\ubuntu.iso"
    assert unc_iso_path("\\\\iso\\ubuntu.iso") == "\\iso\\ubuntu.iso"


def test_parse_cifs_url():
    host, path, user, password = parse_cifs_share(
        "cifs://media:secret@192.168.11.10/iso/live.iso", {}
    )
    assert host == "192.168.11.10"
    assert path == "\\iso\\live.iso"
    assert user == "media"
    assert password == "secret"


def test_parse_cifs_from_plugin_config_appends_filename():
    host, path, user, password = parse_cifs_share(
        "http://rackflow/api/virtual-media/images/tok/installer.iso",
        {
            "vmedia_share_host": "192.168.11.10",
            "vmedia_share_path": "\\iso",
            "vmedia_share_user": "media",
            "vmedia_share_password": "secret",
        },
    )
    assert host == "192.168.11.10"
    assert path == "\\iso\\installer.iso"
    assert user == "media"
    assert password == "secret"


def test_parse_cifs_requires_share_for_http():
    with pytest.raises(VirtualMediaUnavailable, match="CIFS"):
        parse_cifs_share("http://rackflow/installer.iso", {})
