"""HTTP error helpers for API modules (kept out of route files for Sonar S8415)."""

from __future__ import annotations

from typing import TypeVar

from fastapi import HTTPException, status

T = TypeVar("T")


def not_found(detail: str) -> None:
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def bad_request(detail: str) -> None:
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def conflict(detail: str) -> None:
    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def forbidden(detail: str) -> None:
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def service_unavailable(detail: str) -> None:
    raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail)


def require_found(value: T | None, detail: str) -> T:
    if value is None:
        not_found(detail)
    return value
