"""PXE kernel-arg MAC/BOOTIF helpers."""
from types import SimpleNamespace

from app.api.server_interaction import (
    _append_pxe_mac_kernel_args,
    _merge_server_kernel_args,
    _pxe_bootif_from_mac,
    _pxe_colon_mac,
)


def test_pxe_bootif_from_colon_mac():
    assert _pxe_bootif_from_mac("A0:36:9F:80:65:1A") == "01-a0-36-9f-80-65-1a"


def test_pxe_bootif_from_dash_or_bare_mac():
    assert _pxe_bootif_from_mac("a0-36-9f-80-65-1a") == "01-a0-36-9f-80-65-1a"
    assert _pxe_bootif_from_mac("a0369f80651a") == "01-a0-36-9f-80-65-1a"


def test_pxe_bootif_rejects_empty_or_invalid():
    assert _pxe_bootif_from_mac("") == ""
    assert _pxe_bootif_from_mac(None) == ""
    assert _pxe_bootif_from_mac("not-a-mac") == ""


def test_pxe_colon_mac():
    assert _pxe_colon_mac("A0:36:9F:80:65:1A") == "a0:36:9f:80:65:1a"
    assert _pxe_colon_mac("not-a-mac") == ""


def test_append_pxe_mac_kernel_args_when_known():
    port = SimpleNamespace(mac_address="A0:36:9F:80:65:1A")
    out = _append_pxe_mac_kernel_args("boot=live fetch=http://x/fs.squashfs", port)
    assert "BOOTIF=01-a0-36-9f-80-65-1a" in out
    assert "rf_pxe_mac=a0:36:9f:80:65:1a" in out


def test_append_pxe_mac_kernel_args_does_not_duplicate():
    port = SimpleNamespace(mac_address="a0:36:9f:80:65:1a")
    existing = "boot=live BOOTIF=01-a0-36-9f-80-65-1a rf_pxe_mac=a0:36:9f:80:65:1a"
    assert _append_pxe_mac_kernel_args(existing, port) == existing


def test_append_pxe_mac_kernel_args_adds_rf_when_bootif_present():
    port = SimpleNamespace(mac_address="a0:36:9f:80:65:1a")
    out = _append_pxe_mac_kernel_args("BOOTIF=01-a0-36-9f-80-65-1a", port)
    assert out.count("BOOTIF=") == 1
    assert "rf_pxe_mac=a0:36:9f:80:65:1a" in out


def test_append_pxe_mac_kernel_args_skips_without_port():
    assert _append_pxe_mac_kernel_args("boot=live", None) == "boot=live"
    port = SimpleNamespace(mac_address="")
    assert _append_pxe_mac_kernel_args("boot=live", port) == "boot=live"


def test_merge_server_kernel_args_injects_mac(monkeypatch):
    monkeypatch.setattr(
        "app.api.server_interaction._render_kernel_arg_templates",
        lambda value, *a, **k: (value or "").strip(),
    )
    server = SimpleNamespace(pxe_kernel_args_general=None, pxe_kernel_args_network=None)
    port = SimpleNamespace(mac_address="A0:36:9F:80:65:1A")
    out = _merge_server_kernel_args("boot=live", server, port, db=None)
    assert "BOOTIF=01-a0-36-9f-80-65-1a" in out
    assert "rf_pxe_mac=a0:36:9f:80:65:1a" in out
