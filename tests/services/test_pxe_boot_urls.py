"""Tests for LAN PXE boot URL helpers."""
from app.services.pxe_boot_urls import build_pxe_boot_base_url, pxe_boot_api_port, pxe_boot_url_scheme


def test_build_pxe_boot_base_url_defaults_to_lan_http():
    assert build_pxe_boot_base_url("192.168.12.74") == "http://192.168.12.74:8000"


def test_build_pxe_boot_base_url_honors_env(monkeypatch):
    monkeypatch.setenv("PXE_BOOT_URL_SCHEME", "https")
    monkeypatch.setenv("PXE_BOOT_API_PORT", "9443")
    assert pxe_boot_url_scheme() == "https"
    assert pxe_boot_api_port() == 9443
    assert build_pxe_boot_base_url("10.0.0.5") == "https://10.0.0.5:9443"


def test_build_pxe_boot_base_url_rejects_invalid_scheme(monkeypatch):
    monkeypatch.setenv("PXE_BOOT_URL_SCHEME", "ftp")
    assert pxe_boot_url_scheme() == "http"
