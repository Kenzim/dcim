"""Helpers for plugin system tests."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

from app.core.config import settings
from app.models.service import ProvisioningSource, Service, ServiceStatus, ServiceType
from app.models.user import User

EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "plugins"


def run(coro):
    return asyncio.run(coro)


def setup_env(mock_redis, monkeypatch, tmp_path) -> Path:
    """Point the plugin host at a temporary plugins directory and the test database."""
    import app.core.database as db_module
    import app.core.redis as redis_module
    from tests.conftest import TestingSessionLocal, engine

    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(redis_module, "redis_client", mock_redis)
    root = tmp_path / "plugins"
    root.mkdir()
    monkeypatch.setattr(settings, "plugins_dir", str(root))
    return root


def write_plugin(root: Path, manifest: dict, code: str) -> Path:
    path = root / manifest["id"]
    path.mkdir(parents=True, exist_ok=True)
    (path / "plugin.json").write_text(json.dumps(manifest))
    (path / manifest.get("entry", "plugin.py")).write_text(code)
    return path


def copy_example(root: Path, name: str = "uptime_reminder") -> Path:
    target = root / name
    shutil.copytree(EXAMPLES / name, target, ignore=shutil.ignore_patterns("__pycache__"))
    return target


def base_manifest(**overrides) -> dict:
    manifest = {
        "id": "demo_plugin",
        "name": "Demo plugin",
        "version": "1.0.0",
        "rackflow_api": ">=1.0,<2.0",
        "entry": "plugin.py",
        "permissions": [],
    }
    manifest.update(overrides)
    return manifest


def make_user(db, username: str, *, is_admin: bool = False) -> User:
    user = User(username=username, email=f"{username}@example.com", is_admin=is_admin)
    user.set_password("password12345")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def make_service(db, owner: User, *, service_type=ServiceType.VM, status=ServiceStatus.ACTIVE, name="svc") -> Service:
    service = Service(
        name=name,
        service_type=service_type,
        status=status,
        owner_user_id=owner.id,
        provisioning_source=ProvisioningSource.INTERNAL,
    )
    db.add(service)
    db.commit()
    db.refresh(service)
    return service


def install(db, directory: str, *, enable: bool = True):
    from app.plugin_host import lifecycle
    from app.plugin_host.loader import find_package

    package = find_package(directory)
    return run(lifecycle.install(db, directory, accept_permissions=list(package.manifest.permissions), enable=enable))
