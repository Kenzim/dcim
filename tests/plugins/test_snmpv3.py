from app.plugins.snmpv3 import _extract_firmware_version_fallback
import pytest


def test_extract_firmware_version_fallback_with_prefixed_v():
    value = _extract_firmware_version_fallback("SwitchOS v1.2.3-build7, vendor example")
    assert value == "1.2.3-build7"


def test_extract_firmware_version_fallback_without_match():
    value = _extract_firmware_version_fallback("Model ABC firmware unknown token")
    assert value is None


@pytest.mark.parametrize(
    "description,expected",
    [
        ("Vendor OS 12.4.1 build 8", "12.4.1"),
        ("Vendor OS (v2.0.0_rc1); hardware", "2.0.0_rc1"),
        ("release 3.4-beta extra", "3.4-beta"),
        ("release v9.1, extra", "9.1"),
        ("no-version 42 not.a.version?", None),
        ("Version only 2 no dot", None),
        ("bracket [v1.2.3] thing", "1.2.3"),
    ],
)
def test_extract_firmware_version_fallback_cases(description, expected):
    assert _extract_firmware_version_fallback(description) == expected

