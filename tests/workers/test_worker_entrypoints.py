import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.workers import deployment, notification_outbox, recurring_billing, usdt_watcher
from app.services.deployment import worker


@pytest.mark.parametrize("module", [notification_outbox, recurring_billing, usdt_watcher, worker])
def test_make_worker_id(module, monkeypatch):
    monkeypatch.setattr(module.socket, "gethostname", lambda: "host")
    monkeypatch.setattr(module.os, "getpid", lambda: 12)
    monkeypatch.setattr(module.uuid, "uuid4", lambda: MagicMock(hex="abcdef012345"))
    assert module.make_worker_id() == "host-12-abcdef01"


def test_notification_iteration_commits_noop_and_rolls_back_error(monkeypatch):
    db = MagicMock(); monkeypatch.setattr(notification_outbox, "SessionLocal", lambda: db)
    sender = MagicMock(send_batch=MagicMock(return_value=3))
    assert notification_outbox.run_notification_iteration(owner="x", sender=sender) == 3
    db.close.assert_called_once()
    sender.send_batch.side_effect = RuntimeError("bad")
    with pytest.raises(RuntimeError): notification_outbox.run_notification_iteration(owner="x", sender=sender)
    db.rollback.assert_called_once()


@pytest.mark.asyncio
async def test_recurring_iteration_denied_and_acquired(monkeypatch):
    db = MagicMock(); monkeypatch.setattr(recurring_billing, "SessionLocal", lambda: db)
    billing = MagicMock(acquire_lease=MagicMock(return_value=False))
    assert await recurring_billing.run_recurring_billing_iteration(owner="x", service=billing) == {"processed": 0}
    billing.acquire_lease.return_value = True; billing.process_batch = AsyncMock(return_value={"processed": 2})
    assert await recurring_billing.run_recurring_billing_iteration(owner="x", service=billing) == {"processed": 2}
    billing.release_lease.assert_called_once_with(db, owner="x")


@pytest.mark.asyncio
async def test_recurring_iteration_rolls_back_cancellation(monkeypatch):
    db = MagicMock(); monkeypatch.setattr(recurring_billing, "SessionLocal", lambda: db)
    billing = MagicMock(acquire_lease=MagicMock(return_value=True), process_batch=AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await recurring_billing.run_recurring_billing_iteration(owner="x", service=billing)
    db.rollback.assert_called()


def test_usdt_iteration_disabled_and_denied(monkeypatch):
    disabled = MagicMock(usdt_enabled=False)
    assert usdt_watcher.run_usdt_iteration(owner="x", config=disabled) == 0
    db = MagicMock(); monkeypatch.setattr(usdt_watcher, "SessionLocal", lambda: db)
    config = MagicMock(usdt_enabled=True)
    watcher = MagicMock(acquire_lease=MagicMock(return_value=False))
    monkeypatch.setattr(usdt_watcher, "UsdtWatcher", lambda *a: watcher)
    assert usdt_watcher.run_usdt_iteration(owner="x", config=config, rpc=MagicMock()) == 0
    db.close.assert_called_once()


def test_usdt_iteration_processes_releases_and_closes_owned_rpc(monkeypatch):
    db, client = MagicMock(), MagicMock()
    config = MagicMock(usdt_enabled=True)
    watcher = MagicMock(acquire_lease=MagicMock(return_value=True), run_once=MagicMock(return_value=2))
    sweeper = MagicMock(run_once=MagicMock(return_value=3))
    monkeypatch.setattr(usdt_watcher, "SessionLocal", lambda: db)
    monkeypatch.setattr(usdt_watcher, "_rpc_client", lambda _: client)
    monkeypatch.setattr(usdt_watcher, "UsdtWatcher", lambda *a: watcher)
    monkeypatch.setattr(usdt_watcher, "UsdtSweeper", lambda *a: sweeper)
    assert usdt_watcher.run_usdt_iteration(owner="x", config=config) == 5
    watcher.release_lease.assert_called_once(); client.close.assert_called_once()


@pytest.mark.asyncio
async def test_drain_once_handles_job_tick_failure(monkeypatch):
    first, db1, db2 = MagicMock(id=1), MagicMock(), MagicMock()
    sessions = iter([db1, db2]); monkeypatch.setattr(worker, "SessionLocal", lambda: next(sessions))
    monkeypatch.setattr(worker.VMDeploymentJobDAO, "claim_next_runnable", MagicMock(side_effect=[first, None]))
    monkeypatch.setattr(worker, "run_job_tick", AsyncMock(side_effect=RuntimeError("bad")))
    assert await worker._drain_once("owner", 60) == 1
    db1.rollback.assert_called_once(); db1.close.assert_called_once(); db2.close.assert_called_once()


@pytest.mark.asyncio
async def test_deployment_worker_cancels_after_cycle(monkeypatch):
    monkeypatch.setattr(worker, "_log_stale_running_jobs", MagicMock())
    monkeypatch.setattr(worker, "_drain_once", AsyncMock(return_value=0))
    with patch.object(worker.asyncio, "sleep", new=AsyncMock(side_effect=asyncio.CancelledError())):
        with pytest.raises(asyncio.CancelledError):
            await worker.run_deployment_job_worker(worker_id="x")


@pytest.mark.parametrize("module,runner", [
    (deployment, "run_deployment_job_worker"), (notification_outbox, "run_notification_outbox_worker"),
    (recurring_billing, "run_recurring_billing_worker"), (usdt_watcher, "run_usdt_watcher"),
])
def test_entrypoint_swallows_keyboard_interrupt(monkeypatch, module, runner):
    def interrupt(coro):
        coro.close()
        raise KeyboardInterrupt()

    monkeypatch.setattr(module.asyncio, "run", interrupt)
    module.main()
