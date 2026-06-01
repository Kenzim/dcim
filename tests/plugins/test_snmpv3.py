from app.plugins.snmpv3 import _extract_firmware_version_fallback


def test_extract_firmware_version_fallback_with_prefixed_v():
    value = _extract_firmware_version_fallback("SwitchOS v1.2.3-build7, vendor example")
    assert value == "1.2.3-build7"


def test_extract_firmware_version_fallback_without_match():
    value = _extract_firmware_version_fallback("Model ABC firmware unknown token")
    assert value is None

