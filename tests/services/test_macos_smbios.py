from app.services.macos_smbios import generate_smbios, smbios1_config_value


def test_generate_smbios_shape():
    sm = generate_smbios("iMacPro1,1")
    assert sm["SystemProductName"] == "iMacPro1,1"
    assert len(sm["SystemSerialNumber"]) == 12
    assert len(sm["MLB"]) == 17
    assert len(sm["SystemUUID"]) == 36
    assert len(sm["ROM_HEX"]) == 12
    assert "-" in sm["SystemUUID"]


def test_smbios1_config_contains_uuid_and_base64():
    sm = generate_smbios("iMacPro1,1")
    val = smbios1_config_value(sm)
    assert f"uuid={sm['SystemUUID']}" in val
    assert "base64=1" in val
    assert "serial=" in val
