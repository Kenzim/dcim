from fastapi import FastAPI, APIRouter
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from app.core.config import settings

app = FastAPI(
    title=settings.api_title,
    version=settings.api_version,
)

# Create API router with /api prefix
api_router = APIRouter(prefix="/api")

# Include user routes
from app.api import user as user_api
api_router.include_router(user_api.router, prefix="/users", tags=["users"])

# Mount the API router FIRST (before static files)
app.include_router(api_router)

# Mount static files at root (after API routes)
static_path = Path(settings.static_files_path)
if static_path.exists():
    app.mount("/", StaticFiles(directory=str(static_path), html=True), name="static")

