"""macOS SMBIOS generator tests."""

from app.services.macos_smbios import generate_smbios, smbios1_config_value


def test_generate_smbios_shapes_and_model():
    sm = generate_smbios("iMacPro1,1")
    assert sm["SystemProductName"] == "iMacPro1,1"
    assert len(sm["SystemSerialNumber"]) == 12
    assert len(sm["MLB"]) == 17
    assert len(sm["ROM_HEX"]) == 12
    assert sm["SystemSerialNumber"].startswith("C02")

    macpro = generate_smbios("MacPro7,1")
    assert macpro["SystemSerialNumber"].startswith("F5K")


def test_smbios1_config_value_includes_optional_sku():
    sm = generate_smbios()
    base = smbios1_config_value(sm)
    assert "uuid=" in base
    assert "serial=" in base
    assert "sku=" not in base
    with_sku = smbios1_config_value(sm, sku="rf1:tpl=ubuntu")
    assert "sku=" in with_sku
