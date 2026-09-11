"""Unit tests for Rackflow netboot initramfs helpers (fake sysfs)."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
RF_NETBOOT = REPO / "tftp/pxe/temp_os/debian-live/initrd-overlay/lib/rf-netboot.sh"


def _run_rf(args: list[str], env: dict[str, str] | None = None) -> str:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    result = subprocess.run(
        ["sh", str(RF_NETBOOT), *args],
        check=False,
        capture_output=True,
        text=True,
        env=merged,
    )
    if result.returncode != 0:
        pytest.fail(
            f"rf-netboot {' '.join(args)} failed ({result.returncode}): {result.stderr}{result.stdout}"
        )
    return result.stdout.strip()


def _sourced(script: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        ["sh", "-c", f". '{RF_NETBOOT}'\n{script}"],
        check=False,
        capture_output=True,
        text=True,
        env=merged,
    )


def _write_iface(net: Path, name: str, mac: str, carrier: str = "1") -> None:
    d = net / name
    d.mkdir(parents=True)
    (d / "address").write_text(f"{mac}\n")
    (d / "carrier").write_text(f"{carrier}\n")
    (d / "operstate").write_text("up\n" if carrier == "1" else "down\n")


def test_rf_netboot_script_exists():
    assert RF_NETBOOT.is_file()
    init = REPO / "tftp/pxe/temp_os/debian-live/initrd-overlay/init"
    assert init.is_file()
    text = init.read_text()
    assert "rf_netboot_prepare" in text
    assert "live-boot skipped" in text


def test_normalize_mac_formats():
    assert _run_rf(["rf_normalize_mac", "A0:36:9F:80:65:1A"]) == "a0:36:9f:80:65:1a"
    assert _run_rf(["rf_normalize_mac", "a0-36-9f-80-65-1a"]) == "a0:36:9f:80:65:1a"
    assert _run_rf(["rf_normalize_mac", "a0369f80651a"]) == "a0:36:9f:80:65:1a"


def test_mac_from_bootif():
    assert _run_rf(["rf_mac_from_bootif", "01-a0-36-9f-80-65-1a"]) == "a0:36:9f:80:65:1a"
    assert _run_rf(["rf_mac_from_bootif", "01-A0-36-9F-80-65-1A"]) == "a0:36:9f:80:65:1a"


def test_wanted_mac_prefers_rf_pxe_mac():
    env = {
        "RF_CMDLINE": "boot=live rf_pxe_mac=A0:36:9F:80:65:1A BOOTIF=01-00-11-22-33-44-55",
    }
    assert _run_rf(["rf_wanted_mac"], env) == "a0:36:9f:80:65:1a"


def test_wanted_mac_falls_back_to_bootif():
    env = {"RF_CMDLINE": "boot=live BOOTIF=01-a0-36-9f-80-65-1a fetch=http://x/fs.squashfs"}
    assert _run_rf(["rf_wanted_mac"], env) == "a0:36:9f:80:65:1a"


def test_find_iface_skips_lo_and_usb(tmp_path):
    net = tmp_path / "net"
    _write_iface(net, "lo", "00:00:00:00:00:00")
    _write_iface(net, "usb0", "a0:36:9f:80:65:1a")
    _write_iface(net, "eth2", "a0:36:9f:80:65:1a")
    env = {"RF_SYSFS_NET": str(net), "RF_SLEEP": "true"}
    assert _run_rf(["rf_find_iface_by_mac", "a0:36:9f:80:65:1a"], env) == "eth2"


def test_select_iface_waits_for_mac_and_stable_carrier(tmp_path):
    net = tmp_path / "net"
    _write_iface(net, "lo", "00:00:00:00:00:00", "1")
    _write_iface(net, "usb0", "12:34:56:78:9a:bc", "1")
    _write_iface(net, "eth3", "a0:36:9f:80:65:1a", "1")
    env = {
        "RF_SYSFS_NET": str(net),
        "RF_SLEEP": "true",
        "RF_CMDLINE": "rf_pxe_mac=a0:36:9f:80:65:1a",
    }
    proc = _sourced('rf_select_iface 5 2', env)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert proc.stdout.strip().splitlines()[-1] == "eth3"


def _cpio_dir(src: Path, dest: Path) -> None:
    listed = subprocess.check_output(["find", ".", "-print"], cwd=src)
    with dest.open("wb") as out:
        subprocess.run(["cpio", "-o", "-H", "newc", "--quiet"], cwd=src, check=True, input=listed, stdout=out)


def test_pack_script_handles_lib_symlink(tmp_path):
    """Donor /lib is a symlink to usr/lib; overlay helpers must land via that path."""
    pack = REPO / "scripts/debian-live-initrd.sh"
    if not (Path("/usr/bin/zstd").exists() and Path("/usr/bin/cpio").exists()):
        pytest.skip("zstd/cpio required")
    donor_root = tmp_path / "donor"
    (donor_root / "usr/lib").mkdir(parents=True)
    (donor_root / "sbin").mkdir()
    (donor_root / "lib").symlink_to("usr/lib")
    (donor_root / "init").write_text("#!/bin/sh\necho donor\n")
    (donor_root / "sbin/keepme").write_text("keep\n")
    early = tmp_path / "early"
    early.mkdir()
    (early / "ucode").write_text("ucode\n")
    _cpio_dir(early, tmp_path / "e1.cpio")
    _cpio_dir(donor_root, tmp_path / "main.cpio")
    subprocess.run(
        ["zstd", "-c", str(tmp_path / "main.cpio")],
        check=True,
        stdout=(tmp_path / "main.zst").open("wb"),
    )
    donor_img = tmp_path / "donor.img"
    donor_img.write_bytes((tmp_path / "e1.cpio").read_bytes() + (tmp_path / "main.zst").read_bytes())
    overlay = REPO / "tftp/pxe/temp_os/debian-live/initrd-overlay"
    out = tmp_path / "out.img"
    packed = subprocess.run(
        ["bash", str(pack), "pack", "--donor", str(donor_img), "--overlay", str(overlay), "--output", str(out)],
        capture_output=True,
        text=True,
    )
    assert packed.returncode == 0, packed.stderr + packed.stdout
    unpacked = tmp_path / "unpacked"
    subprocess.run(
        ["bash", str(pack), "unpack", "--input", str(out), "--dir", str(unpacked)],
        check=True,
        capture_output=True,
        text=True,
    )
    init_text = (unpacked / "init").read_text()
    assert "rf_netboot_prepare" in init_text
    helper = unpacked / "usr/lib/rf-netboot.sh"
    if not helper.is_file():
        helper = unpacked / "lib/rf-netboot.sh"
    assert helper.is_file()
    assert (unpacked / "sbin/keepme").is_file()


def test_select_iface_skips_usb_even_when_up_first(tmp_path):
    net = tmp_path / "net"
    _write_iface(net, "usb0", "de:ad:be:ef:00:01", "1")
    _write_iface(net, "eth2", "a0:36:9f:80:65:1a", "1")
    env = {
        "RF_SYSFS_NET": str(net),
        "RF_SLEEP": "true",
        "RF_CMDLINE": "BOOTIF=01-a0-36-9f-80-65-1a",
    }
    proc = _sourced('rf_select_iface 5 1', env)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert proc.stdout.strip().splitlines()[-1] == "eth2"
    assert "DEVICE=usb0" not in proc.stdout
