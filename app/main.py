from fastapi import FastAPI, APIRouter
from fastapi.staticfiles import StaticFiles
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

# Include server routes
from app.api import server as server_api
api_router.include_router(server_api.router, prefix="/servers", tags=["servers"])

# Mount the API router FIRST (before static files)
app.include_router(api_router)

# Mount static files at root (after API routes)
static_path = Path(settings.static_files_path)
if static_path.exists():
    app.mount("/", StaticFiles(directory=str(static_path), html=True), name="static")

