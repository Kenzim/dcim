"""The error raised by the NFS storage services."""

from __future__ import annotations


class StorageError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
