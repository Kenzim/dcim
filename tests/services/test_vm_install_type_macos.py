from app.services.vm_install_type_strategy import INSTALL_TYPE_STRATEGIES, list_os_type_schemas


def test_macos_in_install_types():
    assert "macOS - Guest agent" in INSTALL_TYPE_STRATEGIES


def test_list_os_type_schemas_includes_option_fields():
    schemas = list_os_type_schemas()
    by_type = {s["os_type"]: s for s in schemas}
    mac = by_type["macOS - Guest agent"]
    names = {f["name"] for f in mac["option_schema"]}
    assert "randomize_smbios" in names
    assert "network_mode" in names
    assert any(a["name"] == "randomize_smbios" for a in mac["actions"])
