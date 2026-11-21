"""Fixtures for the WHMCS contract tests.

`whmcs` behaves like the WHMCS module: it holds a billing integration API key,
builds services owned by that integration and sends each request the way
`rackflow_apiCall()` does (Bearer key, JSON content type, `json_encode` body).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.dao.billing_integration_dao import BillingIntegrationDAO
from app.dao.ipam_dao import IPAMDAO
from app.dao.location_dao import LocationDAO
from app.dao.proxmox_inventory_dao import ProxmoxInventoryDAO
from app.dao.server_dao import ServerDAO
from app.dao.service_dao import ServiceDAO
from app.dao.user_dao import UserDAO
from app.models.service import ProvisioningSource, ServiceStatus, ServiceType

from tests.contract.whmcs_routes import BY_KEY

NO_BODY = object()


class Whmcs:
    def __init__(self, client, db, *, name: str = "whmcs-contract"):
        self.client = client
        self.db = db
        self.integration = BillingIntegrationDAO.create(db, name=name, integration_type="whmcs")
        self.key = self.integration.plaintext_api_key
        self.headers = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}
        self._n = 0

    def rival(self) -> "Whmcs":
        """A second integration, used to prove services of another integration are invisible."""
        return Whmcs(self.client, self.db, name="whmcs-rival")

    def _next(self) -> int:
        self._n += 1
        return self._n

    def owner(self):
        n = self._next()
        return UserDAO.create(
            self.db,
            username=f"contractuser{n}",
            email=f"contract{n}@example.com",
            billing_integration_id=self.integration.id,
            external_user_id=f"ext-contract-{n}",
            external_username=f"contractuser{n}",
            external_email=f"contract{n}@example.com",
        )

    def cluster(self):
        n = self._next()
        return ProxmoxInventoryDAO.create_cluster(
            self.db,
            name=f"contract-cluster-{n}",
            api_url="https://pve.invalid:8006/",
            username="root@pam",
            password="secret",
        )

    def vm(self, *, placed: bool = True, status=ServiceStatus.ACTIVE, **extra):
        kwargs: dict[str, Any] = {}
        if placed:
            cluster = self.cluster()
            kwargs = {"proxmox_cluster_id": cluster.id, "proxmox_node_name": "pve1", "proxmox_vmid": 101}
        return ServiceDAO.create_vm(
            self.db,
            name=f"contract-vm-{self._next()}",
            owner_user_id=self.owner().id,
            provisioning_source=ProvisioningSource.BILLING,
            status=status,
            **kwargs,
            **extra,
        )

    def server(self, *, ip: str | None = None, **extra):
        n = self._next()
        location = LocationDAO.create(self.db, name=f"contract-loc-{n}")
        return ServerDAO.create(
            self.db,
            name=f"contract-srv-{n}",
            server_ip=ip or f"10.77.0.{n}",
            plugin_name="ipmi",
            plugin_config={},
            location_id=location.id,
            **extra,
        )

    def bare_metal(self, *, status=ServiceStatus.ACTIVE, server=None, permissions=None):
        server = server or self.server()
        service = ServiceDAO.create_bare_metal(
            self.db,
            name=f"contract-bm-{self._next()}",
            server_id=server.id,
            owner_user_id=self.owner().id,
            provisioning_source=ProvisioningSource.BILLING,
            status=status,
            service_type=ServiceType.BARE_METAL,
        )
        return self.with_permissions(service, permissions)

    def with_permissions(self, service, permissions):
        if permissions is not None:
            service.permission_overrides = permissions
            ServiceDAO.update(self.db, service)
        return service

    def proxy(self, *, permissions=None):
        """An http_proxy service with one assigned IP (no rack server)."""
        n = self._next()
        location = LocationDAO.create(self.db, name=f"contract-proxy-loc-{n}")
        subnet = IPAMDAO.create_subnet(
            self.db, name=f"contract-proxy-subnet-{n}", cidr=f"203.0.114.{n * 8}/29", location_id=location.id
        )
        service = ServiceDAO.create_bare_metal(
            self.db,
            name=f"contract-proxy-{n}",
            server_id=None,
            owner_user_id=self.owner().id,
            service_type=ServiceType.HTTP_PROXY,
            status=ServiceStatus.ACTIVE,
            provisioning_source=ProvisioningSource.BILLING,
        )
        IPAMDAO.assign_ip(self.db, service_id=service.id, subnet_id=subnet.id, username="wu", password="wp")
        return self.with_permissions(service, permissions)

    def call(
        self,
        key: str,
        service_id: int | None = None,
        *,
        query: dict | None = None,
        body: Any = NO_BODY,
        headers: dict | None = None,
        **path_params,
    ):
        """Send the request the module sends for route `key`.

        `body` is encoded like PHP's `json_encode`: pass `[]` for the PHP empty
        array (it goes out as `[]`), a dict for an object, or leave it out to send
        no body.
        """
        route = BY_KEY[key]
        if service_id is not None:
            path_params["service_id"] = service_id
        url = "/api" + route.path.format(**path_params)
        kwargs: dict[str, Any] = {"headers": self.headers if headers is None else headers}
        if query:
            kwargs["params"] = query
        if body is not NO_BODY:
            kwargs["content"] = json.dumps(body)
        return self.client.request(route.method, url, **kwargs)


@pytest.fixture
def whmcs(client, db_session) -> Whmcs:
    return Whmcs(client, db_session)
