"""Each detector against recorded state: unhealthy rows are found, healthy ones are not."""

from datetime import datetime, timedelta

import pytest

from app.models.alerting import NotificationDelivery, VmBackupRun
from app.models.boot_task import BootTask, BootTaskStatus
from app.models.commerce_email import EmailMessage, EmailMessageStatus
from app.models.installation_task import InstallationStatus, InstallationTask
from app.models.location import Location
from app.models.network_switch import NetworkSwitch
from app.models.pdu import Pdu, PduInlet
from app.models.power_feed import PowerFeed
from app.models.private_network import AttachmentState, PrivateNetwork, PrivateNetworkAttachment
from app.models.proxy_runner import ProxyRunner
from app.models.rack import Rack
from app.models.reseller import NotificationOutbox
from app.models.runner import Runner
from app.models.server import Server
from app.models.service import Service, ServiceStatus, ServiceType
from app.models.storage import StorageNode, StorageTenant
from app.models.switch_port import SwitchPort
from app.models.vm_deployment_job import DeploymentJobStatus, VMDeploymentJob
from app.services.alerting import detectors as d
from app.services.alerting.catalogue import CATALOGUE
from app.core.config import settings

NOW = datetime.utcnow()


def run(db, name, now=NOW):
    return d.DETECTORS[name](db, CATALOGUE[name].defaults(), now)


def ago(**kw):
    return NOW - timedelta(**kw)


@pytest.fixture
def loc(db_session):
    row = Location(name="DC1", description="")
    db_session.add(row)
    db_session.commit()
    return row


@pytest.fixture
def server(db_session, loc):
    row = Server(name="srv-1", server_ip="10.0.0.1", location_id=loc.id, plugin_name="ipmi", plugin_config={})
    db_session.add(row)
    db_session.commit()
    return row


@pytest.fixture
def service(db_session):
    row = Service(name="vm-1", service_type=ServiceType.VM, status=ServiceStatus.ACTIVE)
    db_session.add(row)
    db_session.commit()
    return row


def test_every_catalogue_type_has_a_detector_except_client_ones():
    staff = {k for k, v in CATALOGUE.items() if v.audience == "staff"}
    assert staff == set(d.DETECTORS)


# ---------------------------------------------------------------- storage


def _node(db, server, **kw):
    node = StorageNode(name="store-1", server_id=server.id, api_key_hash="x", enabled=True, **kw)
    db.add(node)
    db.commit()
    return node


def test_storage_node_offline(db_session, server):
    _node(db_session, server, last_seen_at=ago(hours=1))
    found = run(db_session, "storage_node_offline")
    assert [f.resource_label for f in found] == ["store-1"]
    assert "offline" in found[0].title


def test_storage_node_online_or_new_or_disabled_is_quiet(db_session, server):
    node = _node(db_session, server, last_seen_at=ago(seconds=30))
    assert run(db_session, "storage_node_offline") == []
    node.last_seen_at = None
    db_session.commit()
    assert run(db_session, "storage_node_offline") == []
    node.last_seen_at = ago(hours=2)
    node.enabled = False
    db_session.commit()
    assert run(db_session, "storage_node_offline") == []


def test_storage_node_ceph_only_when_online(db_session, server):
    node = _node(db_session, server, last_seen_at=ago(seconds=20), ceph_ok=False, last_error="HEALTH_ERR password=hunter2")
    found = run(db_session, "storage_node_ceph")
    assert len(found) == 1
    assert "hunter2" not in str(found[0].details)
    node.last_seen_at = ago(hours=1)
    db_session.commit()
    assert run(db_session, "storage_node_ceph") == []


def test_storage_node_error_reports_runner_and_sftp_problems(db_session, server):
    _node(
        db_session, server, last_seen_at=ago(seconds=20), last_error="mount failed",
        sftp_enabled=True, sftp_listening=False,
    )
    found = run(db_session, "storage_node_error")
    assert len(found) == 1
    assert "mount failed" in found[0].message and "SFTP" in found[0].message


def test_storage_tenant_error(db_session, server, service):
    node = _node(db_session, server, last_seen_at=ago(seconds=20))
    tenant = StorageTenant(service_id=service.id, node_id=node.id, size_bytes=1, runner_state="error",
                           runner_error="export failed")
    db_session.add(tenant)
    db_session.commit()
    tenant.updated_at = ago(hours=1)
    db_session.commit()
    found = run(db_session, "storage_tenant_error")
    assert len(found) == 1 and found[0].resource_label == "vm-1"
    tenant.runner_state = "ready"
    tenant.runner_error = None
    db_session.commit()
    assert run(db_session, "storage_tenant_error") == []


# ---------------------------------------------------------------- runners


def test_proxy_runner_offline(db_session):
    db_session.add(ProxyRunner(name="px-1", api_key_encrypted="x", enabled=True, last_seen_at=ago(hours=1)))
    db_session.add(ProxyRunner(name="px-2", api_key_encrypted="x", enabled=True, last_seen_at=ago(seconds=10)))
    db_session.add(ProxyRunner(name="px-3", api_key_encrypted="x", enabled=False, last_seen_at=ago(days=3)))
    db_session.commit()
    assert [f.resource_label for f in run(db_session, "proxy_runner_offline")] == ["px-1"]


def test_runner_offline_uses_hub_redis_and_last_seen(db_session, monkeypatch):
    import app.services.runners.cache as cache

    r1 = Runner(name="dhcp-1", capabilities=["dhcp"], api_key_encrypted="x", enabled=True, last_seen_at=ago(hours=1))
    r2 = Runner(name="tftp-1", capabilities=["tftp"], api_key_encrypted="x", enabled=True, last_seen_at=ago(hours=1))
    r3 = Runner(name="media-1", capabilities=["media"], api_key_encrypted="x", enabled=True, last_seen_at=ago(seconds=5))
    db_session.add_all([r1, r2, r3])
    db_session.commit()
    monkeypatch.setattr(cache, "redis_online", lambda rid: rid == r2.id)
    found = run(db_session, "runner_offline")
    assert [f.resource_id for f in found] == [str(r1.id)]
    assert "dhcp" in found[0].resource_label


def test_runner_offline_survives_redis_failure(db_session, monkeypatch):
    import app.services.runners.cache as cache

    db_session.add(Runner(name="dhcp-1", capabilities=["dhcp"], api_key_encrypted="x", enabled=True,
                          last_seen_at=ago(hours=1)))
    db_session.commit()

    def boom(_):
        raise ConnectionError("redis down")

    monkeypatch.setattr(cache, "redis_online", boom)
    assert len(run(db_session, "runner_offline")) == 1


# ---------------------------------------------------------------- power


def _feed_with_load(db, loc, amps, amperage=30, derate=80):
    rack = Rack(location_id=loc.id, name="R1", units=42)
    db.add(rack)
    db.flush()
    feed = PowerFeed(rack_id=rack.id, name="A", voltage=208, amperage=amperage, max_utilisation_percent=derate)
    db.add(feed)
    pdu = Pdu(name="pdu-a", location_id=loc.id, rack_id=rack.id, plugin_name="x", plugin_config={})
    db.add(pdu)
    db.flush()
    db.add(PduInlet(pdu_id=pdu.id, position=1, name="In 1", power_feed_id=feed.id, current_a=amps, power_w=amps * 208))
    db.commit()
    return feed


def test_power_feed_near_and_over_limit(db_session, loc):
    feed = _feed_with_load(db_session, loc, 10.0)
    assert run(db_session, "power_feed_near_limit") == []
    assert run(db_session, "power_feed_over_limit") == []
    inlet = db_session.query(PduInlet).one()
    inlet.current_a = 22.0  # 91.7% of the 24 A usable limit
    db_session.commit()
    near = run(db_session, "power_feed_near_limit")
    assert len(near) == 1 and near[0].resource_id == str(feed.id)
    assert run(db_session, "power_feed_over_limit") == []
    inlet.current_a = 25.0
    db_session.commit()
    assert len(run(db_session, "power_feed_over_limit")) == 1
    assert run(db_session, "power_feed_near_limit") == []


def test_pdu_poll_failed(db_session, loc):
    stale = Pdu(name="stale", location_id=loc.id, plugin_name="x", plugin_config={}, enabled=True,
                last_polled_at=ago(hours=1), last_poll_error="timeout token=abc123")
    fresh = Pdu(name="fresh", location_id=loc.id, plugin_name="x", plugin_config={}, enabled=True,
                last_polled_at=ago(seconds=30))
    never = Pdu(name="never", location_id=loc.id, plugin_name="x", plugin_config={}, enabled=True)
    off = Pdu(name="off", location_id=loc.id, plugin_name="x", plugin_config={}, enabled=False,
              last_polled_at=ago(days=2))
    db_session.add_all([stale, fresh, never, off])
    db_session.commit()
    found = run(db_session, "pdu_poll_failed")
    assert [f.resource_label for f in found] == ["stale"]
    assert "abc123" not in found[0].message


# ---------------------------------------------------------------- switches


def test_switch_unreachable_uses_newest_port_status(db_session, loc):
    sw = NetworkSwitch(name="tor-1", location_id=loc.id, plugin_name="nxos", plugin_config={}, enabled=True)
    ok = NetworkSwitch(name="tor-2", location_id=loc.id, plugin_name="nxos", plugin_config={}, enabled=True)
    db_session.add_all([sw, ok])
    db_session.flush()
    db_session.add_all(
        [
            SwitchPort(switch_id=sw.id, name="e1", status_updated_at=ago(hours=2)),
            SwitchPort(switch_id=sw.id, name="e2", status_updated_at=ago(hours=1)),
            SwitchPort(switch_id=ok.id, name="e1", status_updated_at=ago(hours=2)),
            SwitchPort(switch_id=ok.id, name="e2", status_updated_at=ago(seconds=60)),
        ]
    )
    db_session.commit()
    assert [f.resource_label for f in run(db_session, "switch_unreachable")] == ["tor-1"]


def test_switch_vpc_inconsistent(db_session, loc):
    a = NetworkSwitch(name="a", location_id=loc.id, plugin_name="nxos", plugin_config={}, enabled=True, vpc_domain_id=10)
    b = NetworkSwitch(name="b", location_id=loc.id, plugin_name="nxos", plugin_config={}, enabled=True, vpc_domain_id=10)
    db_session.add_all([a, b])
    db_session.flush()
    a.vpc_peer_switch_id = b.id
    b.vpc_peer_switch_id = a.id
    db_session.commit()
    assert run(db_session, "switch_vpc_inconsistent") == []
    b.vpc_domain_id = 11
    db_session.commit()
    found = run(db_session, "switch_vpc_inconsistent")
    assert {f.resource_label for f in found} == {"a", "b"}
    b.vpc_domain_id = 10
    b.vpc_peer_switch_id = None
    db_session.commit()
    assert {f.resource_label for f in run(db_session, "switch_vpc_inconsistent")} == {"a"}
    a.vpc_peer_switch_id = None
    a.vpc_error = "detect failed"
    db_session.commit()
    assert {f.resource_label for f in run(db_session, "switch_vpc_inconsistent")} == {"a"}


# ---------------------------------------------------------------- jobs and tasks


def _job(db, service, status, **kw):
    job = VMDeploymentJob(service_id=service.id, strategy_name="restore_backup", status=status, **kw)
    db.add(job)
    db.commit()
    return job


def test_deployment_job_failed_only_latest_and_recent(db_session, service):
    old = _job(db_session, service, DeploymentJobStatus.FAILED, error_message="old", finished_at=ago(hours=2))
    assert len(run(db_session, "deployment_job_failed")) == 1
    _job(db_session, service, DeploymentJobStatus.SUCCEEDED, finished_at=ago(hours=1))
    assert run(db_session, "deployment_job_failed") == []
    old.finished_at = ago(days=5)
    db_session.commit()
    assert run(db_session, "deployment_job_failed") == []


def test_deployment_job_stuck_states(db_session, service):
    queued = _job(db_session, service, DeploymentJobStatus.QUEUED, created_at=ago(hours=1))
    waiting = _job(db_session, service, DeploymentJobStatus.WAITING, created_at=ago(hours=8), started_at=ago(hours=8))
    retrying = _job(db_session, service, DeploymentJobStatus.WAITING, created_at=ago(hours=1),
                    started_at=ago(hours=1), error_message="task failed")
    running = _job(db_session, service, DeploymentJobStatus.RUNNING, created_at=ago(hours=1),
                   started_at=ago(hours=1), locked_at=ago(hours=1))
    fine_running = _job(db_session, service, DeploymentJobStatus.RUNNING, created_at=ago(minutes=5),
                        started_at=ago(minutes=5), locked_at=ago(seconds=20))
    fine_waiting = _job(db_session, service, DeploymentJobStatus.WAITING, created_at=ago(hours=1),
                        started_at=ago(hours=1))
    found = {f.resource_id for f in run(db_session, "deployment_job_stuck")}
    assert found == {str(queued.id), str(waiting.id), str(retrying.id), str(running.id)}
    assert str(fine_running.id) not in found and str(fine_waiting.id) not in found


def test_deployment_job_ignored_for_terminated_service(db_session, service):
    _job(db_session, service, DeploymentJobStatus.QUEUED, created_at=ago(hours=3))
    service.status = ServiceStatus.TERMINATED
    db_session.commit()
    assert run(db_session, "deployment_job_stuck") == []


def test_boot_task_stuck(db_session, server):
    old = BootTask(server_id=server.id, status=BootTaskStatus.PENDING, created_at=ago(hours=2))
    fresh = BootTask(server_id=server.id, status=BootTaskStatus.PENDING, created_at=ago(minutes=2))
    used = BootTask(server_id=server.id, status=BootTaskStatus.PENDING, created_at=ago(hours=2),
                    pxe_nonce_used_at=ago(hours=1))
    ancient = BootTask(server_id=server.id, status=BootTaskStatus.PENDING, created_at=ago(days=30))
    done = BootTask(server_id=server.id, status=BootTaskStatus.COMPLETED, created_at=ago(hours=2))
    db_session.add_all([old, fresh, used, ancient, done])
    db_session.commit()
    found = run(db_session, "boot_task_stuck")
    assert [f.resource_id for f in found] == [str(old.id)]


def _install(db, server, status, **kw):
    bt = BootTask(server_id=server.id, status=BootTaskStatus.COMPLETED)
    db.add(bt)
    db.flush()
    task = InstallationTask(server_id=server.id, boot_task_id=bt.id, status=status, **kw)
    db.add(task)
    db.commit()
    return task


def test_install_failed_and_stuck(db_session, server):
    failed = _install(db_session, server, InstallationStatus.FAILED, error_message="disk", completed_at=ago(hours=1))
    stuck = _install(db_session, server, InstallationStatus.IN_PROGRESS, started_at=ago(hours=5))
    _install(db_session, server, InstallationStatus.IN_PROGRESS, started_at=ago(minutes=10))
    assert [f.resource_id for f in run(db_session, "install_stuck")] == [str(stuck.id)]
    # newer tasks supersede the earlier failure
    assert failed.id and run(db_session, "install_failed") == []


def test_install_failed_when_latest(db_session, server):
    failed = _install(db_session, server, InstallationStatus.FAILED, error_message="disk", completed_at=ago(hours=1))
    found = run(db_session, "install_failed")
    assert [f.resource_id for f in found] == [str(failed.id)]


def test_private_network_error(db_session, loc, service):
    net = PrivateNetwork(name="n1", location_id=loc.id, segment_id=100, cidr="10.9.0.0/24")
    db_session.add(net)
    db_session.flush()
    err = PrivateNetworkAttachment(network_id=net.id, service_id=service.id, state=AttachmentState.ERROR,
                                   last_error="vlan add failed")
    ok = PrivateNetworkAttachment(network_id=net.id, state=AttachmentState.ACTIVE)
    pending_old = PrivateNetworkAttachment(network_id=net.id, state=AttachmentState.PENDING, created_at=ago(hours=1))
    pending_new = PrivateNetworkAttachment(network_id=net.id, state=AttachmentState.PENDING, created_at=ago(seconds=10))
    db_session.add_all([err, ok, pending_old, pending_new])
    db_session.commit()
    found = {f.resource_id for f in run(db_session, "private_network_error")}
    assert found == {str(err.id), str(pending_old.id)}


def test_vm_backup_failed(db_session, service):
    db_session.add(VmBackupRun(service_id=service.id, upid="UPID:a", status="failed", started_at=ago(hours=2),
                               finished_at=ago(hours=1), error="exit 1"))
    db_session.commit()
    assert len(run(db_session, "vm_backup_failed")) == 1
    db_session.add(VmBackupRun(service_id=service.id, upid="UPID:b", status="succeeded", started_at=ago(minutes=30),
                               finished_at=ago(minutes=20)))
    db_session.commit()
    assert run(db_session, "vm_backup_failed") == []


# ---------------------------------------------------------------- notifications


def test_notification_delivery_failing(db_session, monkeypatch):
    monkeypatch.setattr(settings, "smtp_enabled", True)
    assert run(db_session, "notification_delivery_failing") == []
    for i in range(3):
        db_session.add(NotificationOutbox(idempotency_key=f"k{i}", recipient="a@b.c", event="x", template="t",
                                          status="failed"))
    db_session.commit()
    found = run(db_session, "notification_delivery_failing")
    assert len(found) == 1 and "billing notifications failed" in found[0].message


def test_notification_delivery_stuck_queue_and_dead_alert_deliveries(db_session, monkeypatch):
    monkeypatch.setattr(settings, "smtp_enabled", True)
    db_session.add(EmailMessage(to_address="a@b.c", subject="s", status=EmailMessageStatus.QUEUED,
                                created_at=ago(hours=2)))
    for i in range(3):
        db_session.add(NotificationDelivery(idempotency_key=f"dead{i}", channel_kind="webhook", target="x",
                                            event="opened", payload={}, status="dead"))
    db_session.commit()
    found = run(db_session, "notification_delivery_failing")
    assert len(found) == 1
    assert "queued and unsent" in found[0].message and "alert notices failed" in found[0].message
