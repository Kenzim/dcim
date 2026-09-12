"""Shared listing of ISO files under the repo ``isos/`` directory."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from app.services.virtual_media.base import VirtualMediaUnavailable

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ISOS_DIR = _REPO_ROOT / "isos"
_INVALID_ISO_FILENAME_MSG = "Invalid ISO filename"


def isos_dir() -> Path:
    return _ISOS_DIR


def validate_iso_filename(filename: str) -> str:
    name = (filename or "").strip()
    if not name or name != Path(name).name:
        raise VirtualMediaUnavailable(_INVALID_ISO_FILENAME_MSG)
    if ".." in name or "/" in name or "\\" in name or "\x00" in name:
        raise VirtualMediaUnavailable(_INVALID_ISO_FILENAME_MSG)
    if not name.lower().endswith(".iso"):
        raise VirtualMediaUnavailable(_INVALID_ISO_FILENAME_MSG)
    return name


def iso_path(filename: str) -> Path:
    name = validate_iso_filename(filename)
    path = (isos_dir() / name).resolve()
    try:
        path.relative_to(isos_dir().resolve())
    except ValueError as exc:
        raise VirtualMediaUnavailable(_INVALID_ISO_FILENAME_MSG) from exc
    return path


def require_iso_file(filename: str) -> Path:
    path = iso_path(filename)
    if not path.is_file():
        raise VirtualMediaUnavailable(f"ISO file '{filename}' not found")
    return path


def list_iso_files() -> list[dict]:
    """Return catalog rows: filename, size_bytes, size_mb. Sorted by name."""
    directory = isos_dir()
    if not directory.is_dir():
        return []
    rows: list[dict] = []
    for path in directory.iterdir():
        if not path.is_file() or path.suffix.lower() != ".iso":
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        rows.append(
            {
                "filename": path.name,
                "size_bytes": size,
                "size_mb": round(size / (1024 * 1024), 2),
            }
        )
    return sorted(rows, key=lambda row: row["filename"])


def catalog_for_billing() -> list[dict]:
    return [
        {"id": row["filename"], "name": row["filename"], "size_mb": row["size_mb"]}
        for row in list_iso_files()
    ]


def pxe_iso_url(base_url: str, filename: str) -> str:
    return f"{base_url.rstrip('/')}/api/servers/interaction/isos/{filename}"


def optional_filename(value: Optional[str]) -> Optional[str]:
    raw = (value or "").strip()
    return raw or None
