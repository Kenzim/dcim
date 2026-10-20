"""Per-boot iPXE ISO stamping for virtual-media boot."""
import re

import pytest

from app.core.config import settings
from app.services.virtual_media import boot_iso
from app.services.virtual_media.base import VirtualMediaUnavailable

REAL_PLACEHOLDER = boot_iso.PLACEHOLDER.read_bytes()


@pytest.fixture
def fake_template(tmp_path, monkeypatch):
    """A fake 'ISO' holding the real placeholder twice (BIOS + UEFI copies)."""
    iso = b"HEAD" + REAL_PLACEHOLDER + b"MIDDLE" * 100 + REAL_PLACEHOLDER + b"TAIL"
    iso_path = tmp_path / "rackflow-vmedia.iso"
    iso_path.write_bytes(iso)
    monkeypatch.setattr(boot_iso, "TEMPLATE_ISO", iso_path)
    monkeypatch.setattr(boot_iso, "PLACEHOLDER", boot_iso.PLACEHOLDER)
    boot_iso._template_cache.clear()
    monkeypatch.setattr(settings, "public_base_url", "https://rackflow.example.com/")
    return iso_path


def test_placeholder_is_one_iso_block():
    assert len(REAL_PLACEHOLDER) == 2048
    assert REAL_PLACEHOLDER.startswith(b"#!ipxe\n#RACKFLOW-VMEDIA-CONFIG-v1\n")


def test_render_boot_script_chains_https_pxe_with_nonce():
    script = boot_iso.render_boot_script("https://rackflow.example.com/", "N0NCE_abc-123")
    assert script.startswith("#!ipxe\n")
    assert "dhcp" in script
    assert (
        "chain https://rackflow.example.com/api/servers/interaction/pxe?mac=${netX/mac}&t=N0NCE_abc-123"
        in script
    )


def test_stamp_replaces_both_copies_and_keeps_size(fake_template):
    original = fake_template.read_bytes()
    stamped = boot_iso.build_boot_iso("N0NCE_abc-123")
    assert len(stamped) == len(original)
    assert REAL_PLACEHOLDER not in stamped
    assert stamped.count(b"&t=N0NCE_abc-123") == 2
    assert stamped.startswith(b"HEAD#!ipxe\n") and stamped.endswith(b"TAIL")
    # Each copy is still exactly one 2048-byte block.
    starts = [m.start() for m in re.finditer(rb"#!ipxe\necho Rackflow", stamped)]
    assert starts == [4, 4 + 2048 + 600]


def test_stamp_requires_both_placeholders(fake_template):
    fake_template.write_bytes(b"HEAD" + REAL_PLACEHOLDER + b"TAIL")
    boot_iso._template_cache.clear()
    with pytest.raises(VirtualMediaUnavailable, match="placeholders"):
        boot_iso.build_boot_iso("n")


def test_stamp_rejects_oversized_script(fake_template):
    with pytest.raises(VirtualMediaUnavailable, match="does not fit"):
        boot_iso.stamp_boot_iso("#!ipxe\n" + "x" * 3000)


def test_missing_template_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(boot_iso, "TEMPLATE_ISO", tmp_path / "missing.iso")
    boot_iso._template_cache.clear()
    monkeypatch.setattr(settings, "public_base_url", "https://rackflow.example.com")
    with pytest.raises(VirtualMediaUnavailable, match="build-rackflow-netboot-iso"):
        boot_iso.build_boot_iso("n")
    assert boot_iso.vmedia_boot_available() is False


@pytest.mark.parametrize("url", [None, "", "http://rackflow.example.com"])
def test_boot_base_url_requires_https(monkeypatch, url):
    monkeypatch.setattr(settings, "public_base_url", url)
    monkeypatch.setattr(settings, "public_app_url", None)
    with pytest.raises(VirtualMediaUnavailable, match="https"):
        boot_iso.boot_base_url()


def test_boot_iso_filename_is_unguessable():
    a, b = boot_iso.boot_iso_filename(), boot_iso.boot_iso_filename()
    assert a != b
    assert re.fullmatch(r"rfboot-[0-9a-f]{32}\.iso", a)
    assert boot_iso.is_boot_iso_name(a)
    assert not boot_iso.is_boot_iso_name("ubuntu.iso")


def test_catalog_hides_boot_isos(tmp_path, monkeypatch):
    from app.services.virtual_media import iso_catalog

    (tmp_path / "ubuntu.iso").write_bytes(b"x")
    (tmp_path / "rfboot-0123456789abcdef0123456789abcdef.iso").write_bytes(b"x")
    monkeypatch.setattr(iso_catalog, "_ISOS_DIR", tmp_path)
    assert [row["filename"] for row in iso_catalog.list_iso_files()] == ["ubuntu.iso"]
