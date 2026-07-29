"""Durable VM OS identity (smbios1 sku) + Proxmox description notes helpers."""
from __future__ import annotations

import base64
import logging
import re
from typing import Any, Dict, Optional
from urllib.parse import quote, unquote

from sqlalchemy.orm import Session

from app.core.config import settings
from app.dao.product_catalog_dao import VMTemplateDAO
from app.models.product_catalog import VMTemplate
from app.models.service import Service

logger = logging.getLogger(__name__)

RF_TOKEN_PREFIX = "rf1:"
_RF_TOKEN_RE = re.compile(
    r"rf1:(?:tpl=(?P<tpl>[a-z0-9][a-z0-9-]{0,126}[a-z0-9]?))?(?:;svc=(?P<svc>\d+))?",
    re.IGNORECASE,
)
# Proxmox smbios1 string fields when base64=1 is set.
_SMBIOS1_B64_KEYS = ("family", "manufacturer", "product", "serial", "sku", "version")


def build_rf_sku_token(service: Service, *, template_code: Optional[str] = None) -> str:
    """Build ``rf1:tpl={code};svc={id}`` for smbios1 sku / PBS notes."""
    code = (template_code or "").strip()
    if not code and service.vm and service.vm.vm_template_id:
        # Caller may not have loaded the template; code must be passed or resolved.
        pass
    if not code:
        raise ValueError("template_code is required to build an rf1 sku token")
    return f"{RF_TOKEN_PREFIX}tpl={code};svc={int(service.id)}"


def parse_rf_sku_token(sku: Optional[str]) -> Optional[Dict[str, Any]]:
    """Parse ``rf1:tpl=…;svc=…`` from a sku / notes string. Returns None if absent."""
    if not sku:
        return None
    text = str(sku).strip()
    # Proxmox often stores description with percent-encoding (%3A / %0A).
    if "%" in text:
        text = unquote(text)
    # Allow the token to appear anywhere in notes (human title + token).
    match = _RF_TOKEN_RE.search(text)
    if not match:
        return None
    tpl = (match.group("tpl") or "").strip().lower() or None
    svc_raw = match.group("svc")
    service_id = int(svc_raw) if svc_raw else None
    if not tpl and service_id is None:
        return None
    return {"template_code": tpl, "service_id": service_id, "raw": match.group(0)}


def resolve_template_from_token(db: Session, token: Optional[str]) -> Optional[VMTemplate]:
    parsed = parse_rf_sku_token(token)
    if not parsed or not parsed.get("template_code"):
        return None
    return VMTemplateDAO.get_by_code(db, parsed["template_code"])


def _rackflow_public_base() -> str:
    base = (settings.public_base_url or settings.public_app_url or "").strip().rstrip("/")
    return base


def _whmcs_public_base(db: Session, service: Service) -> str:
    owner = getattr(service, "owner_user", None)
    if owner is None or not owner.billing_integration_id:
        return ""
    integration = getattr(owner, "billing_integration", None)
    if integration is None:
        from app.dao.billing_integration_dao import BillingIntegrationDAO

        integration = BillingIntegrationDAO.get_by_id(db, owner.billing_integration_id)
    if integration is None:
        return ""
    cfg = integration.config or {}
    return str(cfg.get("public_base_url") or "").strip().rstrip("/")


def _whmcs_admin_base(db: Session, service: Service) -> str:
    """WHMCS admin UI root (accepts site root or …/admin in public_base_url)."""
    base = _whmcs_public_base(db, service)
    if not base:
        return ""
    if base.lower().endswith("/admin"):
        return base
    return f"{base}/admin"


def build_vm_description_markdown(db: Session, service: Service) -> str:
    """Markdown for Proxmox qemu ``description`` (Notes).

    Human-readable links only. OS identity lives in ``smbios1`` sku (``rf1:…``),
    not in Notes — Proxmox markdown also collapses single newlines, so each field
    is a separate paragraph (blank line between).
    """
    lines: list[str] = []
    product = (service.product_code or "").strip() or "—"
    svc_type = (
        service.service_type.value
        if hasattr(service.service_type, "value")
        else str(service.service_type or "—")
    )
    tmpl = None
    if service.vm and service.vm.vm_template_id:
        tmpl = VMTemplateDAO.get_by_id(db, service.vm.vm_template_id)
    tmpl_code = (tmpl.code if tmpl else "") or "—"
    tmpl_name = (tmpl.name if tmpl else "") or "—"

    lines.append(f"**Service:** {service.name} (#{service.id})")
    lines.append(f"**Product:** {product}")
    lines.append(f"**Type:** {svc_type}")
    lines.append(f"**Template:** {tmpl_name} (`{tmpl_code}`)")

    rf_base = _rackflow_public_base()
    if rf_base:
        lines.append(f"[Open in RackFlow]({rf_base}/admin/services/{service.id})")

    whmcs_admin = _whmcs_admin_base(db, service)
    ext_svc = (service.external_service_id or "").strip()
    owner = getattr(service, "owner_user", None)
    if whmcs_admin and ext_svc:
        lines.append(
            f"[WHMCS service]({whmcs_admin}/clientsservices.php?id={quote(ext_svc, safe='')})"
        )
    if whmcs_admin and owner is not None and owner.external_user_id:
        lines.append(
            f"[WHMCS client]({whmcs_admin}/clientssummary.php?userid={quote(str(owner.external_user_id), safe='')})"
        )

    return "\n\n".join(lines)


def parse_smbios1_fields(smbios1: Optional[str]) -> Dict[str, str]:
    """Parse a Proxmox ``smbios1`` config value into a key→raw-value map."""
    out: Dict[str, str] = {}
    if not smbios1:
        return out
    for part in str(smbios1).split(","):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def get_smbios1_sku(smbios1: Optional[str]) -> Optional[str]:
    fields = parse_smbios1_fields(smbios1)
    raw = fields.get("sku")
    if raw is None or raw == "":
        return None
    if fields.get("base64") in ("1", "true", "True"):
        try:
            return base64.b64decode(raw).decode("utf-8")
        except Exception:
            return raw
    return raw


def set_smbios1_sku(smbios1: Optional[str], sku: str) -> str:
    """Merge ``sku`` into an existing smbios1 string without clobbering other fields.

    Always writes ``sku`` under ``base64=1``. Proxmox rejects plain sku values that
    contain ``;`` / ``:`` (our ``rf1:tpl=…;svc=…`` token), and ``base64=1`` applies
    to all string fields — so plain manufacturer/serial/etc. are promoted too.
    """
    fields = parse_smbios1_fields(smbios1)
    use_b64 = fields.get("base64") in ("1", "true", "True")
    if not use_b64:
        for key in _SMBIOS1_B64_KEYS:
            if key in fields and fields[key]:
                fields[key] = base64.b64encode(fields[key].encode("utf-8")).decode("ascii")
        fields["base64"] = "1"
    fields["sku"] = base64.b64encode(sku.encode("utf-8")).decode("ascii")
    # Preserve a stable-ish order: uuid, base64, then the rest alphabetically.
    ordered: list[str] = []
    if "uuid" in fields:
        ordered.append(f"uuid={fields.pop('uuid')}")
    if "base64" in fields:
        ordered.append(f"base64={fields.pop('base64')}")
    for key in sorted(fields.keys()):
        ordered.append(f"{key}={fields[key]}")
    return ",".join(ordered)


def extract_smbios1_from_config_text(config_text: Optional[str]) -> Optional[str]:
    """Pull ``smbios1: …`` from a qemu-server.conf dump (extractconfig)."""
    if not config_text:
        return None
    for line in str(config_text).splitlines():
        line = line.strip()
        if line.startswith("smbios1:"):
            return line.split(":", 1)[1].strip()
        if line.startswith("smbios1="):
            return line.split("=", 1)[1].strip()
    return None


async def ensure_smbios_sku(plugin, token: str) -> None:
    """Set/merge smbios1 sku on the live VM without clobbering Apple fields."""
    cfg = await plugin.get_qemu_config()
    current = cfg.get("smbios1") if isinstance(cfg, dict) else None
    if not current:
        # Minimal smbios1 with only sku (and uuid if Proxmox assigned one elsewhere).
        merged = set_smbios1_sku("", token)
    else:
        merged = set_smbios1_sku(str(current), token)
    if merged != current:
        await plugin.update_smbios1(merged)


async def stamp_vm_identity(db: Session, service: Service, plugin) -> Optional[str]:
    """Write Proxmox description + smbios1 sku for the service's current template.

    Returns the rf1 token when stamped, else None.
    """
    if not service.vm or not service.vm.vm_template_id:
        logger.info("stamp_vm_identity: service %s has no vm_template_id; skipping", service.id)
        return None
    tmpl = VMTemplateDAO.get_by_id(db, service.vm.vm_template_id)
    if not tmpl or not tmpl.code:
        logger.warning("stamp_vm_identity: template missing code for service %s", service.id)
        return None
    token = build_rf_sku_token(service, template_code=tmpl.code)
    description = build_vm_description_markdown(db, service)
    try:
        if hasattr(plugin, "update_description"):
            await plugin.update_description(description)
        await ensure_smbios_sku(plugin, token)
    except Exception:
        logger.exception("stamp_vm_identity failed for service %s", service.id)
        raise
    return token


def notes_with_rf_token(human_notes: Optional[str], token: str) -> str:
    """Combine optional human backup title with a copy of the smbios sku token.

    OS identity SOT is ``smbios1`` sku. PBS notes keep the same ``rf1:…`` string
    only so list/restore can resolve template without extractconfig. Single line
    (``title · rf1:…``) because PBS notes-template mishandles newlines.
    """
    human = (human_notes or "").strip()
    # Collapse newlines so we never rely on multi-line PBS notes.
    human = " ".join(human.split())
    if human and parse_rf_sku_token(human):
        return human[:1024]
    if human:
        combined = f"{human} · {token}"
    else:
        combined = token
    return combined[:1024]


async def resolve_template_id_for_restore(
    db: Session,
    plugin,
    *,
    explicit_template_id: Optional[int] = None,
    volid: Optional[str] = None,
    notes: Optional[str] = None,
) -> Optional[int]:
    """Resolve VMTemplate id: explicit → live smbios sku → notes → extractconfig."""
    if explicit_template_id is not None:
        tmpl = VMTemplateDAO.get_by_id(db, int(explicit_template_id))
        return tmpl.id if tmpl else None

    # 1) Live guest smbios1 after restore.
    try:
        cfg = await plugin.get_qemu_config()
        sku = get_smbios1_sku(cfg.get("smbios1") if isinstance(cfg, dict) else None)
        tmpl = resolve_template_from_token(db, sku)
        if tmpl:
            return tmpl.id
    except Exception as exc:
        logger.info("Could not read smbios1 after restore: %s", exc)

    # 2) PBS notes token (cheap).
    tmpl = resolve_template_from_token(db, notes)
    if tmpl:
        return tmpl.id

    # 3) extractconfig on the backup volume.
    if volid and hasattr(plugin, "extract_backup_config"):
        try:
            config_text = await plugin.extract_backup_config(volid)
            smbios1 = extract_smbios1_from_config_text(config_text)
            sku = get_smbios1_sku(smbios1)
            tmpl = resolve_template_from_token(db, sku)
            if tmpl:
                return tmpl.id
        except Exception as exc:
            logger.info("extract_backup_config failed for %s: %s", volid, exc)

    return None
