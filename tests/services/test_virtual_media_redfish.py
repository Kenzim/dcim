"""Redfish virtual media helpers (no live BMC)."""
import pytest

from app.services.virtual_media.redfish import _join, image_filename


def test_join_absolute_url_passthrough():
    assert _join("http://bmc", "https://other/path") == "https://other/path"
    assert _join("http://bmc", "http://other/path") == "http://other/path"


def test_join_relative_path():
    assert _join("http://bmc/", "/redfish/v1/Managers/1") == "http://bmc/redfish/v1/Managers/1"


def test_image_filename_from_url():
    assert image_filename("http://rackflow/isos/ubuntu-22.04.iso") == "ubuntu-22.04.iso"
