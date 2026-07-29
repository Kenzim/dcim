import pytest

from app.services.vm_install_type_strategy import INSTALL_TYPE_STRATEGIES, resolve_vm_template_strategy


def test_resolve_cloudinit():
    r = resolve_vm_template_strategy("Linux - Cloudinit")
    assert r["billing_os_code"] == "install-linux-cloudinit"
    assert r["strategy_name"] == "cloudinit_clone"
    assert r["strategy_config"]["use_cloudinit"] is True


def test_resolve_guest_agent():
    r = resolve_vm_template_strategy("Linux - Guest agent")
    assert r["strategy_name"] == "guest_agent"
    assert r["strategy_config"]["use_cloudinit"] is False


def test_resolve_macos_guest_agent():
    r = resolve_vm_template_strategy("macOS - Guest agent")
    assert r["strategy_name"] == "macos_guest_agent"
    assert r["billing_os_code"] == "install-macos-guest-agent"


def test_resolve_windows_guest_agent():
    r = resolve_vm_template_strategy("Windows - Guest agent")
    assert r["strategy_name"] == "windows_guest_agent"
    assert r["billing_os_code"] == "install-windows-guest-agent"
    assert r["strategy_config"]["use_cloudinit"] is False
    assert r["strategy_config"]["provision_mode"] == "clone_then_windows_guest_agent"


def test_allowed_types_cover_mapping():
    assert set(INSTALL_TYPE_STRATEGIES.keys()) == {
        "Linux - Cloudinit",
        "Linux - Guest agent",
        "macOS - Guest agent",
        "Windows - Guest agent",
    }


def test_unknown_install_type():
    with pytest.raises(ValueError, match="Unsupported VM provisioning strategy"):
        resolve_vm_template_strategy("Windows - Other")
