from app.services.deployment.disk_sizing import (
    disk_entry_size_gb,
    needs_grow,
    parse_size_token_to_gb,
    select_primary_disk,
)


def test_parse_size_token_to_gb():
    assert parse_size_token_to_gb("64G") == 64
    assert parse_size_token_to_gb("1T") == 1024
    assert parse_size_token_to_gb("2048M") == 2
    assert parse_size_token_to_gb("") is None


def test_disk_entry_size_gb_skips_cdrom():
    assert disk_entry_size_gb("local:iso/foo.iso,media=cdrom") is None
    assert disk_entry_size_gb("kvm:vm-1-disk-0,size=64G") == 64


def test_select_primary_disk_picks_largest():
    cfg = {
        "ide0": "kvm:vm-disk-efi,size=1G",
        "virtio0": "kvm:vm-disk-data,size=64G",
        "boot": "order=ide0;virtio0",
    }
    assert select_primary_disk(cfg) == ("virtio0", 64.0)


def test_needs_grow():
    assert needs_grow(64, 80) is True
    assert needs_grow(64, 64) is False
    assert needs_grow(80, 64) is False
    assert needs_grow(64, 64.2) is False


def test_macos_grow_already_done_helper():
    from app.services.deployment.guest_config import _macos_grow_already_done

    assert _macos_grow_already_done("Error: -69743: The new size must be different")
    assert not _macos_grow_already_done("Error: -69519: The target disk is too small")
