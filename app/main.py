from fastapi import FastAPI, APIRouter, Request, status, HTTPException
from fastapi.responses import JSONResponse, FileResponse
from fastapi.exceptions import RequestValidationError
from pathlib import Path
from contextlib import asynccontextmanager
from app.core.config import settings
from app.core.redis_notifications import setup_keyspace_notifications, start_keyspace_notification_listener
import asyncio
import logging

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
    
    # Start keyspace notification listener in background
    listener_task = asyncio.create_task(start_keyspace_notification_listener())
    
    # Seed default categories and optional initial admin
    from app.core.database import SessionLocal
    from app.core.seed_categories import seed_categories
    from app.services.plugin_sync import sync_plugins_to_db, sync_switch_plugins_to_db
    from app.dao import UserDAO
    try:
        db = SessionLocal()
        seed_categories(db)
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
    
    listener_task.cancel()
    try:
        await listener_task
    except asyncio.CancelledError:
        pass


app = FastAPI(
    title=settings.api_title,
    version=settings.api_version,
    lifespan=lifespan
)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Handle Pydantic validation errors with detailed logging"""
    logger.error(f"Validation error on {request.method} {request.url.path}")
    
    # Convert errors to JSON-serializable format
    # exc.errors() may contain non-serializable objects (like ValueError) in ctx field
    errors = exc.errors()
    serializable_errors = []
    for error in errors:
        serializable_error = {}
        for key, value in error.items():
            if isinstance(value, Exception):
                # Convert exception objects to strings
                serializable_error[key] = str(value)
            elif isinstance(value, dict):
                # Recursively handle nested dicts (like ctx field)
                serializable_error[key] = {
                    k: str(v) if isinstance(v, Exception) else v
                    for k, v in value.items()
                }
            else:
                serializable_error[key] = value
        serializable_errors.append(serializable_error)
    
    logger.error(f"Validation errors: {serializable_errors}")
    
    try:
        request_body = await request.body()
        body_str = request_body.decode('utf-8', errors='replace') if request_body else ""
    except Exception:
        body_str = ""
    
    logger.error(f"Request body: {body_str}")
    
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "detail": serializable_errors,
            "body": body_str
        }
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

# Include billing admin routes
from app.api import billing_admin as billing_admin_api
api_router.include_router(billing_admin_api.router, tags=["billing-admin"])

# Include services admin routes
from app.api import services_admin as services_admin_api
api_router.include_router(services_admin_api.router, tags=["admin-services"])

# Include scripts admin routes
from app.api import scripts_admin as scripts_admin_api
api_router.include_router(scripts_admin_api.router, tags=["scripts-admin"])

# Include asset manager routes
from app.api import asset as asset_api
api_router.include_router(asset_api.router)

# Mount the API router FIRST (before static files)
app.include_router(api_router)

# SPA fallback: serve static files when they exist, else index.html so client-side routing works (e.g. refresh on /admin)
static_path = Path(settings.static_files_path)


@app.get("/{full_path:path}")
async def serve_spa(full_path: str):
    if full_path.startswith("api/") or full_path == "api":
        raise HTTPException(status_code=404, detail="Not found")
    if not static_path.exists():
        raise HTTPException(status_code=404, detail="Not found")
    # Resolve to a path under static_path (no path escaping)
    file_path = (static_path / full_path).resolve()
    if not str(file_path).startswith(str(static_path.resolve())):
        raise HTTPException(status_code=404, detail="Not found")
    if file_path.is_file():
        return FileResponse(file_path)
    index_file = static_path / "index.html"
    if index_file.is_file():
        return FileResponse(index_file)
    raise HTTPException(status_code=404, detail="Not found")

