"""Parse, validate, and format OpenSSH public keys for VM services."""
from __future__ import annotations

import re
from typing import Any, Iterable, List, Optional, Sequence, Union
from urllib.parse import quote

from sqlalchemy.orm import Session

from app.dao.product_catalog_dao import VMTemplateDAO
from app.models.service import Service
from app.services.vm_install_type_strategy import resolve_vm_template_strategy
from app.services.vm_strategy_executor import resolve_vm_strategy_name_for_service

# Strategies that accept SSH public keys at provision / manage time.
SSH_KEY_STRATEGY_NAMES = frozenset({"cloudinit_clone", "guest_agent"})

_SSH_KEY_LINE_RE = re.compile(
    r"^(ssh-(?:rsa|ed25519|dss)|ecdsa-sha2-nistp(?:256|384|521)|sk-ssh-ed25519@openssh\.com|"
    r"sk-ecdsa-sha2-nistp256@openssh\.com)\s+\S+(?:\s+.*)?$"
)


class SshPublicKeyError(ValueError):
    """Invalid SSH public key input."""


def strategy_accepts_ssh_key(strategy_name: Optional[str]) -> bool:
    return bool(strategy_name) and str(strategy_name) in SSH_KEY_STRATEGY_NAMES


def os_type_accepts_ssh_key(os_type: Optional[str]) -> bool:
    if not os_type:
        return False
    try:
        spec = resolve_vm_template_strategy(str(os_type))
    except ValueError:
        return False
    return strategy_accepts_ssh_key(spec.get("strategy_name"))


def parse_ssh_public_keys(value: Union[str, Sequence[str], None]) -> List[str]:
    """Normalize multiline text or a list into validated OpenSSH public key lines.

    Blank lines are skipped. Raises ``SshPublicKeyError`` on garbage lines.
    """
    if value is None:
        return []
    if isinstance(value, str):
        lines = value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    elif isinstance(value, (list, tuple)):
        lines = []
        for item in value:
            if item is None:
                continue
            lines.extend(str(item).replace("\r\n", "\n").replace("\r", "\n").split("\n"))
    else:
        raise SshPublicKeyError("ssh_public_keys must be a string or list of strings")

    out: List[str] = []
    seen = set()
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not _SSH_KEY_LINE_RE.match(line):
            raise SshPublicKeyError(
                f"Invalid SSH public key line (expected ssh-ed25519/ssh-rsa/ecdsa…): {line[:80]}"
            )
        if line not in seen:
            seen.add(line)
            out.append(line)
    return out


def format_authorized_keys(keys: Iterable[str]) -> str:
    """Join keys with newlines for authorized_keys / Proxmox sshkeys raw form."""
    return "\n".join(str(k).strip() for k in keys if str(k).strip())


def format_ssh_public_keys_text(keys: Iterable[str]) -> str:
    return format_authorized_keys(keys)


def proxmox_sshkeys_param(keys: Sequence[str]) -> Optional[str]:
    """URL-encoded multiline blob for Proxmox ``sshkeys`` config key."""
    blob = format_authorized_keys(keys)
    if not blob:
        return None
    # Proxmox expects percent-encoding with %0A for newlines.
    return quote(blob, safe="")


def ssh_public_keys_from_service_config(config: Optional[dict]) -> List[str]:
    """Read stored keys from ``service.config.template_parameters``."""
    cfg = config if isinstance(config, dict) else {}
    tpl = cfg.get("template_parameters") or {}
    if not isinstance(tpl, dict):
        return []
    if "ssh_public_keys" in tpl:
        try:
            return parse_ssh_public_keys(tpl.get("ssh_public_keys"))
        except SshPublicKeyError:
            # Tolerate legacy/corrupt stored values when reading for display.
            raw = tpl.get("ssh_public_keys")
            if isinstance(raw, list):
                return [str(x).strip() for x in raw if str(x).strip()]
            if isinstance(raw, str) and raw.strip():
                return [ln.strip() for ln in raw.splitlines() if ln.strip()]
            return []
    # Legacy single-key field
    single = tpl.get("ssh_public_key")
    if single:
        try:
            return parse_ssh_public_keys(str(single))
        except SshPublicKeyError:
            return []
    return []


def set_ssh_public_keys_on_service(service: Service, keys: Sequence[str]) -> None:
    """Write normalized key list into ``service.config.template_parameters``."""
    cfg = dict(service.config or {})
    tpl = dict(cfg.get("template_parameters") or {})
    normalized = parse_ssh_public_keys(list(keys))
    if normalized:
        tpl["ssh_public_keys"] = normalized
    else:
        tpl.pop("ssh_public_keys", None)
    tpl.pop("ssh_public_key", None)  # drop legacy singular
    cfg["template_parameters"] = tpl
    service.config = cfg


def service_has_ssh_public_keys(service: Service) -> bool:
    return bool(ssh_public_keys_from_service_config(service.config if isinstance(service.config, dict) else {}))


def service_accepts_ssh_key(db: Session, service: Service, *, target_template_id: Optional[int] = None) -> bool:
    """Whether the service (or a prospective template) accepts SSH keys."""
    if target_template_id is not None:
        tmpl = VMTemplateDAO.get_by_id(db, int(target_template_id))
        if not tmpl:
            return False
        return os_type_accepts_ssh_key(tmpl.os_type)
    name = resolve_vm_strategy_name_for_service(db, service)
    return strategy_accepts_ssh_key(name)


def service_needs_ssh_key_prompt(
    db: Session,
    service: Service,
    *,
    target_template_id: Optional[int] = None,
) -> bool:
    """True when target accepts keys and the service has none stored yet."""
    if not service_accepts_ssh_key(db, service, target_template_id=target_template_id):
        return False
    return not service_has_ssh_public_keys(service)


def ssh_key_fields_for_service(db: Session, service: Service) -> dict[str, Any]:
    """Fields to merge into service API responses."""
    keys = ssh_public_keys_from_service_config(
        service.config if isinstance(service.config, dict) else {}
    )
    accepts = service_accepts_ssh_key(db, service)
    return {
        "accepts_ssh_key": accepts,
        "has_ssh_public_keys": bool(keys),
        "ssh_public_keys_text": format_ssh_public_keys_text(keys) if accepts else "",
        "needs_ssh_key_prompt": service_needs_ssh_key_prompt(db, service),
    }
