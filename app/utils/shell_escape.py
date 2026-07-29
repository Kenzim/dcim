"""Helpers for safely embedding untrusted values into shell script text.

Install/boot scripts are built via literal text substitution (see
``app/api/server_interaction.py`` and ``app/api/billing.py``), not by
passing arguments to a subprocess, so values must be escaped for the shell
*syntax* they land in rather than relying on Python-level quoting.
Templates consistently reference ``PARAM_*`` values inside double-quoted
shell assignments (e.g. ``ADMIN_PASSWORD="${PARAM_ADMIN_PASSWORD}"``), so
escaping for double-quoted-string context prevents both quote breakout and
command/variable substitution, while remaining backward compatible with
every existing template.
"""

import re

# Control characters (excluding tab) that have no legitimate use in a
# template parameter and could otherwise be used to inject extra script
# lines once substituted into the generated install script.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def shell_escape_double_quoted(value) -> str:
    """Escape ``value`` so it is safe to embed inside a double-quoted shell string.

    This is intentionally NOT a substitute for ``shlex.quote`` when building
    a full command line yourself (see ``app/services/deployment/guest_config.py``
    for that pattern) -- it is for template-based textual substitution where
    the surrounding quotes already exist in the template. Escaping
    backslash / backtick / ``$`` / ``"`` prevents the injected value from
    breaking out of a quoted string or triggering command/variable
    substitution, whether it ultimately lands in a quoted or unquoted
    position in the script (a leading backslash strips special meaning from
    the following character in bash in both cases).
    """
    if value is None:
        return ""
    text = str(value)
    # Newlines/control chars have no legitimate use in these parameters
    # (passwords, hostnames, usernames, SSH key comments, ...) and could
    # otherwise inject additional script lines.
    text = _CONTROL_CHARS.sub("", text).replace("\r", "").replace("\n", "")
    # Order matters: escape backslashes first so we don't double-escape the
    # backslashes we introduce for the other characters.
    text = text.replace("\\", "\\\\")
    text = text.replace("`", "\\`")
    text = text.replace("$", "\\$")
    text = text.replace('"', '\\"')
    return text
