"""Virtual media profile registry: normalize, ready, unknown ids."""
from types import SimpleNamespace

import pytest

from app.services.virtual_media.base import VirtualMediaUnavailable
from app.services.virtual_media.registry import (
    list_profiles,
    normalize_profile_id,
    profile_for_server,
    virtual_media_ready,
)


def test_list_profiles_includes_first_pass_vendors():
    ids = {row["id"] for row in list_profiles()}
    assert {"asrockrack", "gigabyte", "supermicro", "supermicro_x9"} <= ids


@pytest.mark.parametrize("value", [None, "", "none", "off", "disabled", "NONE"])
def test_normalize_empty_disables(value):
    assert normalize_profile_id(value) is None


def test_normalize_unknown_raises():
    with pytest.raises(VirtualMediaUnavailable, match="Unknown virtual media profile"):
        normalize_profile_id("redfish")


def test_virtual_media_ready_and_profile_for_server():
    off = SimpleNamespace(virtual_media_profile=None)
    assert virtual_media_ready(off) is False
    assert virtual_media_ready(None) is False

    on = SimpleNamespace(virtual_media_profile="asrockrack")
    assert virtual_media_ready(on) is True
    assert profile_for_server(on).id == "asrockrack"

    with pytest.raises(VirtualMediaUnavailable):
        profile_for_server(off)
