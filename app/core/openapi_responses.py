"""Shared OpenAPI error response schemas for FastAPI route decorators."""

COMMON_ERROR_RESPONSES = {
    400: {"description": "Bad request"},
    403: {"description": "Forbidden"},
    404: {"description": "Not found"},
    409: {"description": "Conflict"},
    503: {"description": "Service unavailable"},
}
