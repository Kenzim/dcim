"""ISO catalog validation helpers."""
import pytest

from app.services.virtual_media.base import VirtualMediaUnavailable
from app.services.virtual_media.iso_catalog import validate_iso_filename


def test_validate_iso_filename_accepts_basename():
    assert validate_iso_filename("rescue.iso") == "rescue.iso"


@pytest.mark.parametrize("name", ["../x.iso", "a/b.iso", "", "readme.txt"])
def test_validate_iso_filename_rejects_invalid(name):
    with pytest.raises(VirtualMediaUnavailable, match="Invalid ISO filename"):
        validate_iso_filename(name)
