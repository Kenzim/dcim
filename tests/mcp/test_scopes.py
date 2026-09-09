import pytest

from app.mcp.scopes import max_scope_rank, normalize_scopes, scope_allows


def test_normalize_defaults_to_read():
    assert normalize_scopes(None) == ["read"]
    assert normalize_scopes([]) == ["read"]


def test_normalize_ladder_order_and_unique():
    assert normalize_scopes(["destructive", "read", "WRITE", "read"]) == [
        "read",
        "write",
        "destructive",
    ]


def test_normalize_rejects_unknown():
    with pytest.raises(ValueError, match="Invalid MCP scope"):
        normalize_scopes(["admin"])


def test_scope_ladder():
    assert scope_allows(["read"], "read")
    assert not scope_allows(["read"], "write")
    assert not scope_allows(["read"], "destructive")
    assert scope_allows(["write"], "read")
    assert scope_allows(["write"], "write")
    assert not scope_allows(["write"], "destructive")
    assert scope_allows(["destructive"], "read")
    assert scope_allows(["destructive"], "write")
    assert scope_allows(["destructive"], "destructive")
    assert max_scope_rank([]) == -1
    assert not scope_allows(["read"], "nope")
