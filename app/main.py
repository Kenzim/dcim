from fastapi import FastAPI, APIRouter, Request, status, HTTPException
from fastapi.responses import JSONResponse, FileResponse
from fastapi.exceptions import RequestValidationError
from pathlib import Path
from contextlib import asynccontextmanager
from app.core.config import settings
from app.core.redis_notifications import setup_keyspace_notifications, start_keyspace_notification_listener
import asyncio
import logging
import re

logger = logging.getLogger(__name__)


def _run_migrations() -> None:
    """Run Alembic migrations so the DB schema exists before the app uses it."""
    from pathlib import Path
    from alembic.config import Config
    from alembic import command
    base = Path(__file__).resolve().parent.parent
    alembic_cfg = Config(str(base / "alembic.ini"))
    command.upgrade(alembic_cfg, "head")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup/shutdown tasks"""
    # Startup
    logger.info("Starting up...")
    _run_migrations()
    setup_keyspace_notifications()
    
    # Start keyspace notification listener (it manages its own background thread)
    start_keyspace_notification_listener()
    from app.services.reconciliation_jobs import run_reconciliation_jobs
    reconciliation_task = asyncio.create_task(run_reconciliation_jobs())

    # Optional in-process deployment worker (single-node/dev). Production runs a
    # dedicated `deployment-worker` container instead; see docker-compose.
    deployment_worker_task = None
    if settings.run_deployment_worker:
        from app.services.deployment.worker import run_deployment_job_worker
        deployment_worker_task = asyncio.create_task(
            run_deployment_job_worker(
                interval_seconds=settings.deployment_worker_interval_seconds,
                lease_ttl_seconds=settings.deployment_worker_lease_ttl_seconds,
            )
        )
        logger.info("In-process deployment worker enabled")

    # Optional in-process USDT watcher for single-node/dev. Production should
    # run the dedicated profile-gated compose service so API replicas never
    # duplicate chain polling.
    usdt_watcher_task = None
    if settings.run_usdt_watcher:
        from app.workers.usdt_watcher import run_usdt_watcher
        usdt_watcher_task = asyncio.create_task(run_usdt_watcher())
        logger.info("In-process USDT watcher enabled")

    recurring_billing_task = None
    if settings.run_recurring_billing_worker:
        from app.workers.recurring_billing import run_recurring_billing_worker

        recurring_billing_task = asyncio.create_task(
            run_recurring_billing_worker()
        )
        logger.info("In-process recurring billing worker enabled")

    notification_outbox_task = None
    if settings.run_notification_outbox_worker:
        from app.workers.notification_outbox import (
            run_notification_outbox_worker,
        )

        notification_outbox_task = asyncio.create_task(
            run_notification_outbox_worker()
        )
        logger.info("In-process notification outbox worker enabled")
    
    # Seed default categories and optional initial admin
    from app.core.database import SessionLocal
    from app.core.seed_categories import seed_categories
    from app.services.plugin_sync import sync_plugins_to_db, sync_switch_plugins_to_db
    from app.dao import UserDAO
    try:
        db = SessionLocal()
        seed_categories(db)
        from app.core.seed_permission_sets import seed_permission_sets
        seed_permission_sets(db)
        # Plugin sync is now a no-op (plugins loaded directly from disk)
        sync_results = sync_plugins_to_db(db)
        switch_sync_results = sync_switch_plugins_to_db(db)
        # Create initial admin if no users exist and env is set
        if (
            settings.initial_admin_username
            and settings.initial_admin_password
            and settings.initial_admin_password.strip()
        ):
            existing = UserDAO.get_all(db, limit=1)
            if not existing:
                email = settings.initial_admin_email or f"{settings.initial_admin_username}@localhost"
                UserDAO.create(
                    db,
                    username=settings.initial_admin_username,
                    email=email,
                    password=settings.initial_admin_password,
                    is_admin=True,
                )
                logger.info("Created initial admin user %s", settings.initial_admin_username)
        db.close()
        logger.info("Seeded default categories")
        logger.info("Plugins are loaded directly from disk (not stored in database)")
    except Exception as e:
        logger.warning(f"Could not seed categories/sync plugins (may already exist): {e}")
    
    yield
    
    # Shutdown
    logger.info("Shutting down...")
    
    reconciliation_task.cancel()
    tasks = [reconciliation_task]
    if deployment_worker_task is not None:
        deployment_worker_task.cancel()
        tasks.append(deployment_worker_task)
    if usdt_watcher_task is not None:
        usdt_watcher_task.cancel()
        tasks.append(usdt_watcher_task)
    if recurring_billing_task is not None:
        recurring_billing_task.cancel()
        tasks.append(recurring_billing_task)
    if notification_outbox_task is not None:
        notification_outbox_task.cancel()
        tasks.append(notification_outbox_task)
    await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(
    title=settings.api_title,
    version=settings.api_version,
    lifespan=lifespan,
    # Unauthenticated schema/docs endpoints leak the full API surface
    # (including admin/billing/reseller routes) to anyone who can reach the
    # server; disable by default and only opt in via explicit config.
    docs_url="/docs" if not settings.disable_public_api_docs else None,
    redoc_url="/redoc" if not settings.disable_public_api_docs else None,
    openapi_url="/openapi.json" if not settings.disable_public_api_docs else None,
)


# Defense-in-depth security headers on every response. The SPA has no known
# {@html}/innerHTML/eval sink today, but a CSP + framing/MIME hardening still
# meaningfully reduces blast radius for any future XSS, and Referrer-Policy
# keeps single-use tokens passed as query params (e.g. the SSO redeem link)
# from leaking to third parties via the Referer header on the post-redirect page.
_CSP_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Content-Security-Policy", _CSP_POLICY)
    return response


# Field names whose submitted value must never be logged or echoed back.
_SENSITIVE_LOC_RE = re.compile(r"password|secret|token|api[_-]?key|credential", re.IGNORECASE)


def _loc_is_sensitive(loc) -> bool:
    for part in loc or ():
        if isinstance(part, str) and _SENSITIVE_LOC_RE.search(part):
            return True
    return False


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Handle Pydantic validation errors without leaking submitted values.

    The raw request body and per-field ``input`` values are intentionally
    dropped: they can contain plaintext passwords or tokens. We return the
    field location, error type, and message only, and redact the message for
    fields whose name looks sensitive.
    """
    safe_errors = []
    for error in exc.errors():
        loc = tuple(error.get("loc", ()))
        sensitive = _loc_is_sensitive(loc)
        safe_errors.append({
            "loc": [str(p) for p in loc],
            "type": error.get("type", "value_error"),
            "msg": "invalid value" if sensitive else str(error.get("msg", "invalid value")),
        })

    error_codes = [(e["type"], "/".join(e["loc"])) for e in safe_errors]
    logger.warning(
        "Validation error on %s %s: %s",
        request.method,
        request.url.path,
        error_codes,
    )

    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": safe_errors},
    )

# Create API router with /api prefix
api_router = APIRouter(prefix="/api")

# Include user routes
from app.api import user as user_api
api_router.include_router(user_api.router, prefix="/users", tags=["users"])

# Include plugin routes
from app.api import plugin as plugin_api
api_router.include_router(plugin_api.router, prefix="/plugins", tags=["plugins"])

# Include location routes
from app.api import location as location_api
api_router.include_router(location_api.router, prefix="/locations", tags=["locations"])

# Include service instance routes (per-location DHCP/TFTP runner registration)
from app.api import service_instance as service_instance_api
api_router.include_router(service_instance_api.router)

# Include location-scoped DHCP/TFTP routes
from app.api import location_dhcp as location_dhcp_api
from app.api import location_tftp as location_tftp_api
api_router.include_router(location_dhcp_api.router)
api_router.include_router(location_tftp_api.router)

# Include rack routes
from app.api import rack as rack_api
api_router.include_router(rack_api.router, prefix="/racks", tags=["racks"])

# Include network switch routes
from app.api import network_switch as network_switch_api
api_router.include_router(network_switch_api.router, prefix="/network-switches", tags=["network-switches"])

# Include cable run routes
from app.api import cable_run as cable_run_api
api_router.include_router(cable_run_api.router, prefix="/cable-runs", tags=["cable-runs"])

# Include switch plugin routes
from app.api import switch_plugin as switch_plugin_api
api_router.include_router(switch_plugin_api.router, prefix="/switch-plugins", tags=["switch-plugins"])

# Include server routes
from app.api import server as server_api
api_router.include_router(server_api.router, prefix="/servers", tags=["servers"])

# Include server group routes
from app.api import server_group as server_group_api
api_router.include_router(server_group_api.router, prefix="/server-groups", tags=["server-groups"])

# Include server interaction routes (PXE boot, network config, password updates, etc.)
from app.api import server_interaction as server_interaction_api
api_router.include_router(server_interaction_api.router, prefix="/servers/interaction", tags=["server-interaction"])

# Include installation task routes
from app.api import installation_tasks as installation_tasks_api
api_router.include_router(installation_tasks_api.router, tags=["installation-tasks"])

# Include utility routes
from app.api import utils as utils_api
api_router.include_router(utils_api.router, tags=["utils"])

# Include OS template routes
from app.api import os_templates as os_templates_api
api_router.include_router(os_templates_api.router, prefix="/os-templates", tags=["os-templates"])

# Include billing API routes
from app.api import billing as billing_api
api_router.include_router(billing_api.router, tags=["billing"])

# Include reseller API routes (dedicated reseller key authentication)
from app.api import reseller as reseller_api
api_router.include_router(reseller_api.router, tags=["reseller"])

# Include session-authenticated reseller billing/payment routes and the
# signature-verified Stripe webhook.
from app.api import reseller_panel as reseller_panel_api
api_router.include_router(reseller_panel_api.router, tags=["reseller-panel"])

# Include reseller administration routes
from app.api import reseller_admin as reseller_admin_api
api_router.include_router(reseller_admin_api.router, tags=["reseller-admin"])

# Include billing admin routes
from app.api import billing_admin as billing_admin_api
api_router.include_router(billing_admin_api.router, tags=["billing-admin"])

# Include MCP API key admin routes (works even when /mcp is disabled)
from app.api import mcp_keys_admin as mcp_keys_admin_api
api_router.include_router(mcp_keys_admin_api.router, tags=["mcp-admin"])

# Include product catalog admin routes
from app.api import product_catalog as product_catalog_api
api_router.include_router(product_catalog_api.router, tags=["product-catalog"])

# Include Proxmox inventory routes
from app.api import proxmox_inventory as proxmox_inventory_api
api_router.include_router(proxmox_inventory_api.router, tags=["proxmox"])

# Include IPAM routes
from app.api import ipam as ipam_api
api_router.include_router(ipam_api.router, tags=["ipam"])

# Include proxy runner config routes
from app.api import runner_proxy as runner_proxy_api
api_router.include_router(runner_proxy_api.router, tags=["proxy-runner"])

from app.api import proxy_runners as proxy_runners_api
api_router.include_router(proxy_runners_api.router, tags=["proxy-runners"])

from app.api import proxy_subnet_groups as proxy_subnet_groups_api
api_router.include_router(proxy_subnet_groups_api.router, tags=["proxy-subnet-groups"])

# IPMI reverse-proxy runner API (config + ticket redeem)
from app.api import runner_ipmi as runner_ipmi_api
api_router.include_router(runner_ipmi_api.router, tags=["ipmi-proxy-runner"])

# Include VM IP allocation routes
from app.api import vm_ip_allocations as vm_ip_allocations_api
api_router.include_router(vm_ip_allocations_api.router, tags=["vm-ip-allocations"])

# Include services admin routes
from app.api import services_admin as services_admin_api
api_router.include_router(services_admin_api.router, tags=["admin-services"])

# Include admin clients (client portal accounts) and admins (staff) routes
from app.api import admin_users as admin_users_api
api_router.include_router(admin_users_api.clients_router, tags=["admin-clients"])
api_router.include_router(admin_users_api.admins_router, tags=["admin-admins"])

# Include client services routes
from app.api import services_client as services_client_api
api_router.include_router(services_client_api.router, tags=["services-client"])

# Include dedicated client portal routes (login/logout/me + client-scoped
# services), mounted at /api/client so the edge (e.g. Cloudflare Access) can
# bypass SSO for this prefix only while every other /api/* route stays
# behind Access.
from app.api import client as client_api
api_router.include_router(client_api.router, tags=["client"])

# Include scripts admin routes
from app.api import scripts_admin as scripts_admin_api
api_router.include_router(scripts_admin_api.router, tags=["scripts-admin"])

# Include asset manager routes
from app.api import asset as asset_api
api_router.include_router(asset_api.router)

# Include client permission preset admin routes
from app.api import permission_sets_admin as permission_sets_admin_api
api_router.include_router(permission_sets_admin_api.router, tags=["admin-permission-sets"])

# VM guest VNC console: public launch-ticket redeem + WebSocket bridge to Proxmox
from app.api import vm_vnc as vm_vnc_api
api_router.include_router(vm_vnc_api.router, tags=["vm-vnc"])

# IPMI HTML5 KVM: profile list + public redeem / asset proxy / WS bridge to BMC
from app.api import ipmi_kvm as ipmi_kvm_api
api_router.include_router(ipmi_kvm_api.profiles_router, tags=["ipmi-kvm"])
api_router.include_router(ipmi_kvm_api.router, tags=["ipmi-kvm"])

# Mount the API router FIRST (before static files)
app.include_router(api_router)

# MCP Streamable HTTP (before SPA catch-all). Feature-flagged: when off, /mcp 404s
# for every method (the SPA fallback is GET-only, so POST would otherwise 405).
if settings.mcp_enabled:
    from app.mcp.server import get_mcp_asgi_app

    app.mount("/mcp", get_mcp_asgi_app())
else:

    async def _mcp_disabled(full_path: str = ""):
        raise HTTPException(status_code=404, detail="Not found")

    _mcp_off_methods = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
    app.add_api_route(
        "/mcp",
        _mcp_disabled,
        methods=_mcp_off_methods,
        include_in_schema=False,
    )
    app.add_api_route(
        "/mcp/{full_path:path}",
        _mcp_disabled,
        methods=_mcp_off_methods,
        include_in_schema=False,
    )

# SPA fallback: serve static files when they exist, else index.html so client-side routing works (e.g. refresh on /admin)
static_path = Path(settings.static_files_path)


@app.get("/{full_path:path}")
async def serve_spa(full_path: str):
    if (
        full_path.startswith("api/")
        or full_path == "api"
        or full_path.startswith("mcp/")
        or full_path == "mcp"
    ):
        raise HTTPException(status_code=404, detail="Not found")
    if not static_path.exists():
        raise HTTPException(status_code=404, detail="Not found")
    # Resolve to a path under static_path (no path escaping). Using
    # relative_to() against the resolved base (rather than a naive string
    # startswith()) avoids false-positive containment for sibling directories
    # that merely share a prefix (e.g. "/var/www/static-evil" vs "/var/www/static").
    resolved_static_path = static_path.resolve()
    file_path = (static_path / full_path).resolve()
    try:
        file_path.relative_to(resolved_static_path)
    except ValueError:
        raise HTTPException(status_code=404, detail="Not found")
    if file_path.is_file():
        return FileResponse(file_path)
    index_file = static_path / "index.html"
    if index_file.is_file():
        return FileResponse(index_file)
    raise HTTPException(status_code=404, detail="Not found")

