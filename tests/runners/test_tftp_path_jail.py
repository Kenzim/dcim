"""Security tests for the TFTP runner's put_config path confinement."""
import base64
import importlib
import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def tftp_client(tmp_path, monkeypatch):
    """Import the tftp_runner app with a temp root and auth disabled for testing."""
    root = tmp_path / "tftp"
    root.mkdir()
    monkeypatch.setenv("TFTP_ROOT", str(root))
    monkeypatch.setenv("ALLOW_UNAUTHENTICATED", "true")
    # Reload the module so it picks up the patched environment.
    import tftp_runner.main as tftp_main
    tftp_main = importlib.reload(tftp_main)
    with TestClient(tftp_main.app) as client:
        yield client, root


def _body(path: str, text: str = "data"):
    return {"path": path, "content": base64.b64encode(text.encode()).decode()}


def test_put_config_writes_within_root(tftp_client):
    client, root = tftp_client
    resp = client.put("/config", json=_body("pxelinux.cfg/default", "hello"))
    assert resp.status_code == 200
    assert (root / "pxelinux.cfg/default").read_text() == "hello"


def test_put_config_rejects_parent_traversal(tftp_client):
    client, root = tftp_client
    resp = client.put("/config", json=_body("../escape.txt", "x"))
    assert resp.status_code == 400
    assert not (root.parent / "escape.txt").exists()


def test_put_config_rejects_absolute_path(tftp_client, tmp_path):
    client, root = tftp_client
    target = tmp_path / "outside.txt"
    resp = client.put("/config", json=_body(str(target), "x"))
    assert resp.status_code == 400
    assert not target.exists()


def test_put_config_rejects_sibling_prefix(tftp_client):
    """A sibling dir sharing the root's name prefix must not be writable."""
    client, root = tftp_client
    # e.g. root=/tmp/.../tftp ; try /tmp/.../tftp-evil/x
    sibling = root.parent / (root.name + "-evil") / "x.txt"
    resp = client.put("/config", json=_body(str(sibling), "x"))
    assert resp.status_code == 400
    assert not sibling.exists()


def test_ensure_bios_ipxe_copied_to_root(tftp_client):
    client, root = tftp_client
    src = root / "pxe" / "undionly.kpxe"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"ipxe-undi")
    import tftp_runner.main as tftp_main

    tftp_main.ensure_bios_ipxe_at_root()
    dest = root / "undionly.kpxe"
    assert dest.read_bytes() == b"ipxe-undi"


def test_ensure_bios_ipxe_skips_when_source_missing(tftp_client):
    client, root = tftp_client
    import tftp_runner.main as tftp_main

    tftp_main.ensure_bios_ipxe_at_root()
    assert not (root / "undionly.kpxe").exists()
