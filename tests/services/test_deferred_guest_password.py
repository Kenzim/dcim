"""Tests for deferred Change Password (persist + sync apply + background job)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.dao.service_dao import ServiceDAO
from app.dao.vm_deployment_job_dao import VMDeploymentJobDAO
from app.models.product_catalog import VMTemplate
from app.models.service import ProvisioningSource, ServiceStatus
from app.models.service_vm import VMGuestState
from app.models.vm_deployment_job import DeploymentJobStatus
from app.plugins.base import PowerState
from app.services.deployment.context import DeploymentContext
from app.services.deployment.guest_config import strategy_options_from_ctx
from app.services.deployment.registry import get_deployment_strategy_registry
from app.services.deployment.runner import run_job_tick
from app.services.deployment.strategies import ApplyGuestPasswordStrategy
from app.services.strategy_actions import run_action
from app.services.vm_strategy_executor import (
    APPLY_GUEST_PASSWORD_STRATEGY,
    enqueue_apply_guest_password_job,
)


def _run(coro):
    return asyncio.run(coro)


def test_apply_guest_password_strategy_registered():
    strategy = get_deployment_strategy_registry().get(APPLY_GUEST_PASSWORD_STRATEGY)
    assert isinstance(strategy, ApplyGuestPasswordStrategy)
    assert strategy.mutates_provision_lifecycle is False
    assert strategy.step_names() == [
        "power_on",
        "wait_for_guest_agent",
        "apply_guest_password",
    ]


def test_strategy_options_prefers_template_parameters_password():
    service = SimpleNamespace(
        config={
            "vm_plan": {
                "effective_specs": {
                    "admin_password": "stale-from-provision",
                    "guest_username": "client",
                }
            },
            "template_parameters": {"admin_password": "fresh-from-whmcs"},
        },
        product_snapshot=None,
    )
    ctx = SimpleNamespace(
        service=service,
        get_specs=lambda: dict(service.config["vm_plan"]["effective_specs"]),
    )
    opts = strategy_options_from_ctx(ctx)
    assert opts["guest_password"] == "fresh-from-whmcs"
    assert opts["admin_password"] == "fresh-from-whmcs"


def _vm_service(db_session, *, strategy_name="macos_guest_agent", password="oldpass"):
    tmpl = VMTemplate(
        code="macos-tpl",
        name="T",
        os_type="macOS - Guest agent",
        proxmox_template_name="macos-tpl",
    )
    db_session.add(tmpl)
    db_session.commit()
    db_session.refresh(tmpl)
    service = ServiceDAO.create_vm(
        db_session,
        name="vm-pw-1",
        provisioning_source=ProvisioningSource.INTERNAL,
        status=ServiceStatus.ACTIVE,
        vm_template_id=tmpl.id,
        proxmox_cluster_id=1,
        proxmox_node_name="pve1",
    )
    service.vm.proxmox_vmid = 5100
    service.vm.guest_state = VMGuestState.RUNNING
    service.config = {
        "vm_plan": {"strategy_name": strategy_name, "effective_specs": {}},
        "template_parameters": {"admin_password": password},
    }
    ServiceDAO.update(db_session, service)
    return service


def _patch_change_password_path(monkeypatch, *, agent_ready=False, set_ok=True):
    plugin = MagicMock()
    plugin.guest_agent_ready = AsyncMock(return_value=agent_ready)

    async def _set(*args, **kwargs):
        if not set_ok:
            raise RuntimeError("Failed to set password for 'client'")
        return None

    monkeypatch.setattr(
        "app.services.strategy_actions._plugin_for_service",
        lambda db, service: plugin,
    )
    monkeypatch.setattr(
        "app.services.strategy_actions.set_guest_password",
        _set,
    )
    monkeypatch.setattr(
        "app.services.strategy_actions._SYNC_PASSWORD_WAIT_SECONDS",
        0.05,
    )
    monkeypatch.setattr(
        "app.services.strategy_actions._SYNC_PASSWORD_POLL_INTERVAL",
        0.01,
    )
    monkeypatch.setattr(
        "app.services.strategy_actions.list_actions",
        lambda db, service, audience: [{"name": "change_password"}],
    )
    monkeypatch.setattr(
        "app.services.strategy_actions.resolve_vm_strategy_name_for_service",
        lambda db, service: "macos_guest_agent",
    )
    monkeypatch.setattr(
        "app.services.strategy_actions.get_deployment_strategy_registry",
        lambda: get_deployment_strategy_registry(),
    )
    return plugin


def test_change_password_persists_and_defers_when_agent_down(db_session, monkeypatch):
    service = _vm_service(db_session, password="oldpass")
    _patch_change_password_path(monkeypatch, agent_ready=False)

    result = _run(
        run_action(db_session, service, "change_password", {"password": "newpass"}, "admin")
    )

    assert result["status"] == "ok"
    assert result["applied"] is False
    assert result["pending"] is True
    assert result["job_id"] is not None

    db_session.refresh(service)
    tpl = (service.config or {}).get("template_parameters") or {}
    assert tpl["admin_password"] == "newpass"
    assert tpl["previous_admin_password"] == "oldpass"
    assert (service.config or {}).get("guest_password_apply", {}).get("status") == "pending"

    job = VMDeploymentJobDAO.get_by_id(db_session, result["job_id"])
    assert job is not None
    assert job.strategy_name == APPLY_GUEST_PASSWORD_STRATEGY
    assert job.status == DeploymentJobStatus.QUEUED


def test_change_password_applies_sync_when_agent_ready(db_session, monkeypatch):
    service = _vm_service(db_session, password="oldpass")
    _patch_change_password_path(monkeypatch, agent_ready=True)

    result = _run(
        run_action(db_session, service, "change_password", {"password": "newpass"}, "admin")
    )

    assert result["applied"] is True
    assert result["pending"] is False
    assert result.get("job_id") is None
    db_session.refresh(service)
    assert (service.config or {})["template_parameters"]["admin_password"] == "newpass"
    assert (service.config or {})["guest_password_apply"]["status"] == "applied"
    assert VMDeploymentJobDAO.get_active_for_service(db_session, service.id) is None


def test_change_password_skips_enqueue_when_provision_active(db_session, monkeypatch):
    service = _vm_service(db_session, password="oldpass")
    _patch_change_password_path(monkeypatch, agent_ready=False)

    provision = VMDeploymentJobDAO.create_job(
        db_session,
        service_id=service.id,
        strategy_name="macos_guest_agent",
        step_names=["clone_from_template", "power_on"],
    )

    result = _run(
        run_action(db_session, service, "change_password", {"password": "newpass"}, "admin")
    )

    assert result["pending"] is True
    assert result["job_id"] is None
    db_session.refresh(service)
    assert (service.config or {})["template_parameters"]["admin_password"] == "newpass"
    active = VMDeploymentJobDAO.get_active_for_service(db_session, service.id)
    assert active is not None
    assert active.id == provision.id


def test_enqueue_apply_reuses_existing_password_job(db_session):
    service = _vm_service(db_session)
    first = enqueue_apply_guest_password_job(db_session, service)
    second = enqueue_apply_guest_password_job(db_session, service)
    assert first is not None
    assert second is not None
    assert first.id == second.id


def test_apply_password_job_failure_does_not_mark_guest_error(db_session, monkeypatch):
    service = _vm_service(db_session, password="desired")
    service.vm.guest_state = VMGuestState.RUNNING
    ServiceDAO.update(db_session, service)

    job = enqueue_apply_guest_password_job(db_session, service)
    assert job is not None

    class BoomPlugin:
        async def get_power_state(self):
            return PowerState.ON

        async def power_on(self):
            return True

        async def guest_agent_ready(self):
            return True

        async def guest_set_user_password(self, *args, **kwargs):
            raise RuntimeError("agent refused")

    monkeypatch.setattr(DeploymentContext, "get_plugin", lambda self: BoomPlugin())

    final = None
    for _ in range(20):
        claimed = VMDeploymentJobDAO.claim_next_runnable(
            db_session, worker_id="w1", lease_ttl_seconds=60
        )
        if claimed is None:
            break
        _run(run_job_tick(db_session, claimed))
        db_session.expire_all()
        final = VMDeploymentJobDAO.get_by_id(db_session, job.id)
        if final.status in (
            DeploymentJobStatus.SUCCEEDED,
            DeploymentJobStatus.FAILED,
            DeploymentJobStatus.CANCELLED,
        ):
            break

    db_session.refresh(service)
    assert final is not None
    assert final.status == DeploymentJobStatus.FAILED
    assert service.vm.guest_state == VMGuestState.RUNNING
    apply_summary = (service.config or {}).get("guest_password_apply") or {}
    assert apply_summary.get("status") == "failed"
    assert (service.config or {}).get("vm_provision") is None
