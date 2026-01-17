"""
Tests for MAC address normalization in server interaction API
"""
import pytest
from app.api.server_interaction import normalize_mac_address


def test_normalize_mac_address_colon_format():
    """Test normalization of colon-separated MAC address"""
    result = normalize_mac_address("00:0e:1e:6f:16:b0")
    assert result == "00:0E:1E:6F:16:B0"


def test_normalize_mac_address_dash_format():
    """Test normalization of dash-separated MAC address"""
    result = normalize_mac_address("00-0e-1e-6f-16-b0")
    assert result == "00:0E:1E:6F:16:B0"


def test_normalize_mac_address_no_separators():
    """Test normalization of MAC address without separators"""
    result = normalize_mac_address("000e1e6f16b0")
    assert result == "00:0E:1E:6F:16:B0"


def test_normalize_mac_address_mixed_case():
    """Test normalization of mixed case MAC address"""
    result = normalize_mac_address("00:0E:1e:6F:16:b0")
    assert result == "00:0E:1E:6F:16:B0"


def test_normalize_mac_address_dot_format():
    """Test normalization of dot-separated MAC address"""
    result = normalize_mac_address("00.0e.1e.6f.16.b0")
    assert result == "00:0E:1E:6F:16:B0"


def test_normalize_mac_address_empty_string():
    """Test that empty string raises ValueError"""
    with pytest.raises(ValueError, match="cannot be None or empty"):
        normalize_mac_address("")


def test_normalize_mac_address_none():
    """Test that None raises ValueError"""
    with pytest.raises(ValueError, match="cannot be None or empty"):
        normalize_mac_address(None)


def test_normalize_mac_address_invalid_length():
    """Test that invalid length MAC address returns uppercase version"""
    # If MAC is not exactly 12 hex chars, it should still uppercase it
    result = normalize_mac_address("00:0e:1e:6f:16")
    assert result == "00:0E:1E:6F:16"
