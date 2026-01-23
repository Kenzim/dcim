from fastapi import FastAPI, APIRouter, Request, status
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from pathlib import Path
from contextlib import asynccontextmanager
from app.core.config import settings
from app.core.redis_notifications import setup_keyspace_notifications, start_keyspace_notification_listener
import asyncio
import logging

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup/shutdown tasks"""
    # Startup
    logger.info("Starting up...")
    setup_keyspace_notifications()
    
    # Start keyspace notification listener in background
    listener_task = asyncio.create_task(start_keyspace_notification_listener())
    
    # Seed default categories if needed
    from app.core.database import SessionLocal
    from app.core.seed_categories import seed_categories
    from app.services.plugin_sync import sync_plugins_to_db
    try:
        db = SessionLocal()
        seed_categories(db)
        # Auto-sync plugins to database on startup
        sync_results = sync_plugins_to_db(db)
        db.close()
        logger.info("Seeded default categories")
        logger.info(f"Synced plugins: {len(sync_results['created'])} created, {len(sync_results['updated'])} updated")
    except Exception as e:
        logger.warning(f"Could not seed categories/sync plugins (may already exist): {e}")
    
    yield
    
    # Shutdown
    logger.info("Shutting down...")
    
    # Stop DHCP service if running
    from app.services.dhcp_service import get_dhcp_service
    try:
        dhcp_service = get_dhcp_service()
        await dhcp_service.cleanup()
    except Exception as e:
        logger.warning(f"Error stopping DHCP service: {e}")
    
    # Stop TFTP service if running
    from app.services.tftp_service import get_tftp_service
    try:
        tftp_service = get_tftp_service()
        await tftp_service.cleanup()
    except Exception as e:
        logger.warning(f"Error stopping TFTP service: {e}")
    
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

# Include rack routes
from app.api import rack as rack_api
api_router.include_router(rack_api.router, prefix="/racks", tags=["racks"])

# Include server routes
from app.api import server as server_api
api_router.include_router(server_api.router, prefix="/servers", tags=["servers"])

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

# Include DHCP management routes
from app.api import dhcp as dhcp_api
api_router.include_router(dhcp_api.router, tags=["dhcp"])

# Include TFTP management routes
from app.api import tftp as tftp_api
api_router.include_router(tftp_api.router, tags=["tftp"])

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

# Mount the API router FIRST (before static files)
app.include_router(api_router)

# Mount static files at root (after API routes)
static_path = Path(settings.static_files_path)
if static_path.exists():
    app.mount("/", StaticFiles(directory=str(static_path), html=True), name="static")

