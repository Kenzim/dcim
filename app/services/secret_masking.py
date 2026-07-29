"""Helpers for masking sensitive server/plugin credentials in API responses.

``Server.plugin_config`` (BMC/hypervisor credentials) and
``Server.ipmi_viewer_password`` are stored in plaintext and were previously
returned verbatim to any admin session via ``GET /api/servers`` and
``GET /api/servers/{id}``. These helpers mask secret values on the way out
and transparently restore them on the way back in when the admin UI submits
the mask placeholder unchanged, so editing a server never has to display
(or risk clobbering) the real stored secret.
"""
from typing import Any, Dict, Optional

from app.plugins.registry import get_registry

# Fixed-length placeholder so the response never leaks the real secret's length.
MASKED_SECRET_PLACEHOLDER = "\u2022" * 8


def _password_field_names(plugin_name: Optional[str]) -> set:
    """Return the CONFIG_TEMPLATE property names declared with format=password."""
    if not plugin_name:
        return set()
    plugin_class = get_registry().get_plugin_class(plugin_name)
    if not plugin_class:
        return set()
    properties = (getattr(plugin_class, "CONFIG_TEMPLATE", None) or {}).get("properties", {})
    return {
        key
        for key, schema in properties.items()
        if isinstance(schema, dict) and schema.get("format") == "password"
    }


def mask_plugin_config(plugin_name: Optional[str], plugin_config: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Return a copy of plugin_config with any password-format fields masked."""
    if not plugin_config:
        return plugin_config
    secret_fields = _password_field_names(plugin_name)
    if not secret_fields:
        return plugin_config
    masked = dict(plugin_config)
    for field in secret_fields:
        if masked.get(field):
            masked[field] = MASKED_SECRET_PLACEHOLDER
    return masked


def restore_masked_plugin_config(
    plugin_name: Optional[str],
    incoming_config: Optional[Dict[str, Any]],
    existing_config: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Undo masking on write.

    If a password-format field in ``incoming_config`` is still the mask
    placeholder (the admin didn't change it), restore the real value from
    ``existing_config`` so we don't overwrite the stored secret with the
    placeholder string itself.
    """
    if incoming_config is None:
        return None
    secret_fields = _password_field_names(plugin_name)
    if not secret_fields or not existing_config:
        return incoming_config
    restored = dict(incoming_config)
    for field in secret_fields:
        if restored.get(field) == MASKED_SECRET_PLACEHOLDER:
            restored[field] = existing_config.get(field)
    return restored
