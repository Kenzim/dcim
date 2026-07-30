"""Unit tests for shell_escape_double_quoted."""
import pytest

from app.utils.shell_escape import shell_escape_double_quoted


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, ""),
        ("", ""),
        ("plain", "plain"),
        ("has space", "has space"),
        ("a\\b", "a\\\\b"),
        ("a`b", "a\\`b"),
        ("a$b", "a\\$b"),
        ('say "hi"', 'say \\"hi\\"'),
        ("line\nbreak", "linebreak"),
        ("line\rbreak", "linebreak"),
        ("a\x00b\x07c", "abc"),
        ("keep\ttab", "keep\ttab"),
        ("café", "café"),
        ("$()", "\\$()"),
        ('`"\\', '\\`\\"\\\\'),
        ("admin'pass", "admin'pass"),
        ("$(rm -rf /)", "\\$(rm -rf /)"),
        ('"; rm -rf /; echo "', '\\"; rm -rf /; echo \\"'),
        ("`id`", "\\`id\\`"),
        ("pass\nword\x1f!", "password!"),
    ],
)
def test_shell_escape_double_quoted(value, expected):
    assert shell_escape_double_quoted(value) == expected


def test_escape_order_backslash_before_specials():
    # Backslash must be escaped first so later escapes stay single-level.
    assert shell_escape_double_quoted('\\$"') == '\\\\\\$\\"'


def test_result_cannot_close_double_quoted_assignment():
    raw = 'x"; evil; echo "'
    escaped = shell_escape_double_quoted(raw)
    # Quote/metacharacters are escaped so bash treats them as literals inside "".
    assert escaped == 'x\\"; evil; echo \\"'
    assert shell_escape_double_quoted('$(id)') == '\\$(id)'
