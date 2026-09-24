"""Unified location runners that phone home over a persistent WebSocket.

Import hub/protocol/cache modules directly; this package init stays empty
to avoid circular imports (DAO → protocol → package → hub → DAO).
"""
