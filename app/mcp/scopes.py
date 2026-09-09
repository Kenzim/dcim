"""MCP key scope ladder: read ⊂ write ⊂ destructive.

Importable from tests without spinning up FastMCP.
"""

from __future__ import annotations

from typing import Iterable

VALID_SCOPES = ("read", "write", "destructive")
SCOPE_RANK = {"read": 0, "write": 1, "destructive": 2}


def normalize_scopes(scopes: Iterable[str] | None) -> list[str]:
    """Return unique valid scopes in ladder order. Empty defaults to read."""
    seen: set[str] = set()
    for raw in scopes or ():
        name = (raw or "").strip().lower()
        if name not in SCOPE_RANK:
            raise ValueError(f"Invalid MCP scope: {raw!r}. Use read, write, or destructive.")
        seen.add(name)
    if not seen:
        seen.add("read")
    return [name for name in VALID_SCOPES if name in seen]


def max_scope_rank(granted: Iterable[str] | None) -> int:
    ranks = [SCOPE_RANK[s] for s in granted or () if s in SCOPE_RANK]
    return max(ranks) if ranks else -1


def scope_allows(granted: Iterable[str] | None, required: str) -> bool:
    """True if the key's highest granted scope meets ``required`` on the ladder."""
    needed = SCOPE_RANK.get((required or "").strip().lower())
    if needed is None:
        return False
    return max_scope_rank(granted) >= needed
