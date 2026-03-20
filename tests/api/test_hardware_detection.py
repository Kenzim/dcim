from app.dao import BootTaskDAO, DiskDAO, NetworkPortDAO
from app.dao.hardware_detection_report_dao import HardwareDetectionReportDAO
from app.models.boot_task import BootType
from app.models.cable_run import CableRun
from app.models.hardware_detection_report import HardwareDetectionReportStatus
from app.models.location import Location
from app.models.network_switch import NetworkSwitch
from app.models.server import Server
from app.models.switch_port import SwitchPort
from app.services.download_token_service import get_download_token_service


def _admin_headers(client, test_admin_user):
    response = client.post(
        "/api/users/login",
        json={"username": test_admin_user.username, "password": "adminpassword123"},
    )
    assert response.status_code == 200
    token = response.json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _create_server_with_pxe_port(db_session, name: str = "hw-server", ip: str = "10.50.0.10"):
    location = Location(name=f"{name}-loc", description="test")
    db_session.add(location)
    db_session.commit()
    db_session.refresh(location)

    server = Server(
        name=name,
        server_ip=ip,
        location_id=location.id,
        plugin_name="ipmi",
        plugin_config={"hostname": ip},
        cpu_count=4,
        cpu_model="Intel Xeon Old",
        ram_gb=16,
    )
    db_session.add(server)
    db_session.commit()
    db_session.refresh(server)

    pxe_port = NetworkPortDAO.create(
        db_session,
        server_id=server.id,
        name="eth0",
        mac_address="AA:BB:CC:00:00:01",
        speed_mbps=1000,
        pxe_boot=True,
        pxe_ip=ip,
        pci_address="0000:01:00.0",
        is_physical=True,
        monitor_bandwidth=True,
    )
    return server, location, pxe_port


def test_run_hardware_detection_queues_boot_task_and_pending_report(client, db_session, test_admin_user, monkeypatch):
    headers = _admin_headers(client, test_admin_user)
    server, _, _ = _create_server_with_pxe_port(db_session, name="queue-server", ip="10.50.0.20")

    class FakeTempOSService:
        def get_os_config(self, os_id):
            if os_id == "debian-live":
                return {"id": "debian-live"}
            return None

        def get_kernel_url(self, os_id, base_url):
            return f"{base_url}/kernel"

        def get_initrd_url(self, os_id, base_url):
            return f"{base_url}/initrd"

        def get_kernel_params(self, os_id):
            return "ip=dhcp"

        def get_squashfs_url(self, os_id, base_url):
            return f"{base_url}/filesystem.squashfs"

    monkeypatch.setattr("app.api.server.get_temp_os_service", lambda: FakeTempOSService())

    response = client.post(f"/api/servers/{server.id}/hardware-detection/run", headers=headers)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "pending"
    assert payload["report_id"] > 0
    assert payload["boot_task_id"] > 0

    boot_task = BootTaskDAO.get_by_id(db_session, payload["boot_task_id"])
    assert boot_task is not None
    assert boot_task.boot_type == BootType.TEMP_OS
    assert "hardware-detection/report" in (boot_task.script_content or "")

    report = HardwareDetectionReportDAO.get_by_id(db_session, payload["report_id"])
    assert report is not None
    assert report.status == HardwareDetectionReportStatus.PENDING
    assert report.boot_task_id == boot_task.id


def test_ingest_hardware_detection_report_submits_payload(client, db_session):
    server, _, _ = _create_server_with_pxe_port(db_session, name="ingest-server", ip="10.50.0.30")
    boot_task = BootTaskDAO.create(
        db=db_session,
        server_id=server.id,
        boot_type=BootType.TEMP_OS,
        temp_os_id="debian-live",
        description="Hardware detection",
    )
    report = HardwareDetectionReportDAO.create(
        db_session,
        server_id=server.id,
        boot_task_id=boot_task.id,
        status=HardwareDetectionReportStatus.PENDING,
    )
    token = get_download_token_service().generate_token(
        boot_task_id=boot_task.id,
        allowed_patterns=["hardware-detection-report-*"],
        single_use=False,
    )

    response = client.post(
        f"/api/servers/interaction/hardware-detection/report?token={token}",
        json={
            "cpu_count": 16,
            "cpu_model": "AMD EPYC 9354",
            "ram_gb": 128,
            "nics": [
                {
                    "name": "ens1f0",
                    "mac_address": "AA:BB:CC:10:00:01",
                    "speed_mbps": 25000,
                    "pci_address": "0000:18:00.0",
                    "is_physical": True,
                }
            ],
            "disks": [
                {
                    "name": "nvme0n1",
                    "serial_number": "DISK-001",
                    "model": "Samsung PM9A3",
                    "capacity_gb": 1920,
                    "type": "ssd",
                }
            ],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["report_id"] == report.id

    updated = HardwareDetectionReportDAO.get_by_id(db_session, report.id)
    assert updated.status == HardwareDetectionReportStatus.SUBMITTED
    assert updated.detected_inventory["cpu"]["count"] == 16
    assert updated.detected_inventory["nics"][0]["pci_address"] == "0000:18:00.0"


def test_hardware_detection_diff_apply_and_reject_flow(client, db_session, test_admin_user):
    headers = _admin_headers(client, test_admin_user)
    server, _, _ = _create_server_with_pxe_port(db_session, name="diff-apply-server", ip="10.50.0.40")
    DiskDAO.create(
        db_session,
        server_id=server.id,
        type="hdd",
        capacity_gb=1000,
        model="Old Disk",
        serial_number="OLD-DISK-1",
        description="old",
        is_os_disk=False,
    )

    report = HardwareDetectionReportDAO.create(
        db_session,
        server_id=server.id,
        status=HardwareDetectionReportStatus.PENDING,
    )
    HardwareDetectionReportDAO.mark_submitted(
        db_session,
        report=report,
        source_ip="10.50.0.40",
        detected_inventory={
            "cpu": {"count": 8, "model": "Intel Xeon Gold 6348"},
            "ram_gb": 64,
            "nics": [
                {
                    "name": "ens2f0",
                    "mac_address": "aa:bb:cc:00:00:01",
                    "speed_mbps": 10000,
                    "model": "Intel X710",
                    "pci_address": "0000:01:00.0",
                    "is_physical": True,
                }
            ],
            "disks": [
                {
                    "name": "nvme0n1",
                    "serial_number": "NEW-DISK-1",
                    "model": "Micron 7450",
                    "capacity_gb": 1920,
                    "type": "ssd",
                    "is_os_disk": True,
                }
            ],
        },
    )

    diff_response = client.get(
        f"/api/servers/{server.id}/hardware-detection/reports/{report.id}/diff",
        headers=headers,
    )
    assert diff_response.status_code == 200, diff_response.text
    diff = diff_response.json()["diff"]
    assert diff["server"]["changed"] is True

    apply_response = client.post(
        f"/api/servers/{server.id}/hardware-detection/reports/{report.id}/apply",
        headers=headers,
        json={"nic_remap": {}, "notes": "Reviewed and applied"},
    )
    assert apply_response.status_code == 200, apply_response.text
    assert apply_response.json()["status"] == "applied"

    refreshed_server = db_session.query(Server).filter(Server.id == server.id).first()
    assert refreshed_server.cpu_count == 8
    assert refreshed_server.ram_gb == 64
    refreshed_report = HardwareDetectionReportDAO.get_by_id(db_session, report.id)
    assert refreshed_report.status == HardwareDetectionReportStatus.APPLIED

    reject_report = HardwareDetectionReportDAO.create(
        db_session,
        server_id=server.id,
        status=HardwareDetectionReportStatus.PENDING,
    )
    HardwareDetectionReportDAO.mark_submitted(
        db_session,
        report=reject_report,
        source_ip="10.50.0.40",
        detected_inventory={"cpu": {"count": 4, "model": "Ignore"}, "ram_gb": 8, "nics": [], "disks": []},
    )

    reject_response = client.post(
        f"/api/servers/{server.id}/hardware-detection/reports/{reject_report.id}/reject",
        headers=headers,
        json={"notes": "Not trusted"},
    )
    assert reject_response.status_code == 200, reject_response.text
    assert reject_response.json()["status"] == "rejected"


def test_apply_requires_remap_for_connected_stale_nic_and_preserves_cable(client, db_session, test_admin_user):
    headers = _admin_headers(client, test_admin_user)
    server, location, old_port = _create_server_with_pxe_port(db_session, name="remap-server", ip="10.50.0.50")

    switch = NetworkSwitch(
        name="remap-switch",
        location_id=location.id,
        plugin_name="snmp",
        plugin_config={"host": "10.50.0.51"},
    )
    db_session.add(switch)
    db_session.commit()
    db_session.refresh(switch)

    switch_port = SwitchPort(switch_id=switch.id, name="Gi0/1")
    db_session.add(switch_port)
    db_session.commit()
    db_session.refresh(switch_port)

    cable = CableRun(
        end_a_server_port_id=old_port.id,
        end_b_switch_port_id=switch_port.id,
    )
    db_session.add(cable)
    db_session.commit()
    db_session.refresh(cable)

    report = HardwareDetectionReportDAO.create(
        db_session,
        server_id=server.id,
        status=HardwareDetectionReportStatus.PENDING,
    )
    HardwareDetectionReportDAO.mark_submitted(
        db_session,
        report=report,
        source_ip="10.50.0.50",
        detected_inventory={
            "cpu": {"count": 4, "model": "Intel Xeon Old"},
            "ram_gb": 16,
            "nics": [
                {
                    "name": "ens7f0",
                    "mac_address": "aa:bb:cc:ff:00:01",
                    "speed_mbps": 10000,
                    "model": "Broadcom 57414",
                    "pci_address": "0000:81:00.0",
                    "is_physical": True,
                }
            ],
            "disks": [],
        },
    )

    without_remap = client.post(
        f"/api/servers/{server.id}/hardware-detection/reports/{report.id}/apply",
        headers=headers,
        json={"nic_remap": {}},
    )
    assert without_remap.status_code == 409

    with_remap = client.post(
        f"/api/servers/{server.id}/hardware-detection/reports/{report.id}/apply",
        headers=headers,
        json={"nic_remap": {str(old_port.id): 0}},
    )
    assert with_remap.status_code == 200, with_remap.text

    db_session.refresh(cable)
    assert cable.end_a_server_port_id != old_port.id
    remapped_port = NetworkPortDAO.get_by_id(db_session, cable.end_a_server_port_id)
    assert remapped_port is not None
    assert remapped_port.pci_address == "0000:81:00.0"
