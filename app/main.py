from fastapi import FastAPI
from app.core.config import settings

app = FastAPI(
    title=settings.api_title,
    version=settings.api_version,
)

