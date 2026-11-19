"""SFTP access to NFS storage: users, credentials, share access modes and the runner's view of them.

Rackflow keeps only bcrypt hashes of SFTP passwords and the users' public keys.
The storage runner receives those (never a password) and serves SFTP through a
front end that routes each login to that storage's own locked-down back end.
See ``docs/nfs-storage-sftp.md``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import re
import secrets
from datetime import datetime, timezone
from typing import Iterable, List, Optional

import bcrypt
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from cryptography.hazmat.primitives.serialization import load_ssh_public_key
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, object_session

from app.core.config import settings
from app.dao.storage_dao import StorageDAO
from app.models.service import ServiceStatus
from app.models.storage import SftpAccess, StorageNode, StorageSftpUser, StorageTenant
from app.services.storage_location import endpoint_for_tenant

MAX_SFTP_USERS = 16
MAX_KEYS_PER_USER = 16
MAX_ALLOWED_IPS = 32
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_BYTES = 72
MIN_RSA_BITS = 2048
BCRYPT_ROUNDS = 12
SFTP_USER_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
SFTP_RUNNER_STATES = {"ready", "pending", "error", "off"}
KEY_TYPES = {
    "ssh-ed25519",
    "ssh-rsa",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
}
_COMMENT_RE = re.compile(r"[^\x20-\x7e]")


class SftpError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def sftp_login(tenant: StorageTenant, user: StorageSftpUser) -> str:
    """The SSH login name: unique on every node because it carries the service ID."""
    return f"{user.name}.{tenant.service_id}"


def access_value(raw) -> SftpAccess:
    if isinstance(raw, SftpAccess):
        return raw
    try:
        return SftpAccess(str(raw or SftpAccess.OFF.value).strip().lower())
    except ValueError:
        raise SftpError("sftp_access must be 'off', 'ro' or 'rw'") from None


# --- credentials ---


def parse_public_key(line: str) -> dict:
    """Validate one OpenSSH public key line and return ``{key, type, fingerprint, comment}``."""
    parts = str(line or "").strip().split(None, 2)
    if len(parts) < 2:
        raise SftpError("A public key must look like 'ssh-ed25519 AAAA... comment'")
    key_type, blob_b64 = parts[0], parts[1]
    if key_type not in KEY_TYPES:
        raise SftpError(f"Key type {key_type[:40]!r} is not supported; use ed25519, ECDSA or RSA")
    try:
        blob = base64.b64decode(blob_b64, validate=True)
        key = load_ssh_public_key(f"{key_type} {blob_b64}".encode("ascii"))
    except (binascii.Error, ValueError, UnicodeEncodeError):
        raise SftpError("A public key is not valid") from None
    if isinstance(key, rsa.RSAPublicKey):
        if key.key_size < MIN_RSA_BITS:
            raise SftpError(f"RSA keys must have at least {MIN_RSA_BITS} bits")
    elif not isinstance(key, (ed25519.Ed25519PublicKey, ec.EllipticCurvePublicKey)):
        raise SftpError("A public key is not valid")
    comment = _COMMENT_RE.sub("", parts[2]).strip()[:100] if len(parts) > 2 else ""
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")
    text = f"{key_type} {blob_b64}" + (f" {comment}" if comment else "")
    return {"key": text, "type": key_type, "fingerprint": fingerprint, "comment": comment or None}


def parse_public_keys(lines: Optional[Iterable[str]]) -> List[dict]:
    out: List[dict] = []
    seen = set()
    for line in lines or []:
        if not str(line or "").strip():
            continue
        parsed = parse_public_key(line)
        if parsed["fingerprint"] in seen:
            continue
        seen.add(parsed["fingerprint"])
        out.append(parsed)
    if len(out) > MAX_KEYS_PER_USER:
        raise SftpError(f"An SFTP user can have at most {MAX_KEYS_PER_USER} keys")
    return out


def hash_password(password: str) -> str:
    raw = str(password or "").encode("utf-8")
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise SftpError(f"The password must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(raw) > MAX_PASSWORD_BYTES:
        raise SftpError(f"The password must be at most {MAX_PASSWORD_BYTES} bytes")
    return bcrypt.hashpw(raw, bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode("ascii")


def generate_password() -> str:
    return secrets.token_urlsafe(18)


def parse_allowed_ips(values: Optional[Iterable[str]]) -> List[str]:
    out: List[str] = []
    for raw in values or []:
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            network = ipaddress.ip_network(text, strict=False)
        except ValueError:
            raise SftpError(f"'{text[:64]}' is not a valid IP address or CIDR") from None
        if str(network) not in out:
            out.append(str(network))
    if len(out) > MAX_ALLOWED_IPS:
        raise SftpError(f"An SFTP user can list at most {MAX_ALLOWED_IPS} source ranges")
    return out


# --- users ---


def _require_manageable(tenant: StorageTenant) -> None:
    service = tenant.service
    if tenant.terminated_at is not None or service is None or service.status == ServiceStatus.TERMINATED:
        raise SftpError("Storage is terminated", 409)
    if service.status == ServiceStatus.SUSPENDED:
        raise SftpError("Storage is suspended", 409)


def require_user(tenant: StorageTenant, user_id: int) -> StorageSftpUser:
    for user in tenant.sftp_users:
        if user.id == user_id:
            return user
    raise SftpError("SFTP user not found", 404)


def _password_change(password: Optional[str], generate: bool) -> tuple[Optional[str], Optional[str]]:
    """``(hash, generated_plaintext)`` for a requested password, or ``(None, None)``."""
    if generate:
        plaintext = generate_password()
        return hash_password(plaintext), plaintext
    if password is not None:
        return hash_password(password), None
    return None, None


def create_user(
    db: Session,
    tenant: StorageTenant,
    *,
    name: str,
    public_keys: Optional[List[str]] = None,
    password: Optional[str] = None,
    generate_password: bool = False,
    allowed_ips: Optional[List[str]] = None,
) -> tuple[StorageSftpUser, Optional[str]]:
    """Create an SFTP user. Returns the user and a generated password (shown once) if asked for."""
    _require_manageable(tenant)
    name = (name or "").strip()
    if not SFTP_USER_NAME_RE.match(name):
        raise SftpError("User name must start with a letter and use 1-32 lowercase letters, digits, '-' or '_'")
    if any(user.name == name for user in tenant.sftp_users):
        raise SftpError("An SFTP user with this name already exists", 409)
    if len(tenant.sftp_users) >= MAX_SFTP_USERS:
        raise SftpError(f"A storage can have at most {MAX_SFTP_USERS} SFTP users", 409)
    keys = parse_public_keys(public_keys)
    password_hash, generated = _password_change(password, generate_password)
    if not keys and password_hash is None:
        raise SftpError("Give the user at least one public key or a password")
    user = StorageSftpUser(
        tenant_id=tenant.id,
        name=name,
        public_keys=keys,
        password_hash=password_hash,
        allowed_ips=parse_allowed_ips(allowed_ips),
        enabled=True,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise SftpError("An SFTP user with this name already exists", 409) from None
    db.refresh(user)
    db.refresh(tenant)
    return user, generated


def update_user(
    db: Session,
    tenant: StorageTenant,
    user_id: int,
    *,
    public_keys: Optional[List[str]] = None,
    password: Optional[str] = None,
    generate_password: bool = False,
    clear_password: bool = False,
    allowed_ips: Optional[List[str]] = None,
    enabled: Optional[bool] = None,
) -> tuple[StorageSftpUser, Optional[str]]:
    _require_manageable(tenant)
    user = require_user(tenant, user_id)
    if clear_password and (password is not None or generate_password):
        raise SftpError("Choose either a new password or clear_password, not both")
    keys = parse_public_keys(public_keys) if public_keys is not None else list(user.public_keys or [])
    password_hash, generated = _password_change(password, generate_password)
    if password_hash is None and not clear_password:
        password_hash = user.password_hash
    if not keys and password_hash is None:
        raise SftpError("The user would have no public key and no password left")
    user.public_keys = keys
    user.password_hash = password_hash
    if allowed_ips is not None:
        user.allowed_ips = parse_allowed_ips(allowed_ips)
    if enabled is not None:
        user.enabled = bool(enabled)
    return StorageDAO.save(db, user), generated


def delete_user(db: Session, tenant: StorageTenant, user_id: int) -> None:
    if tenant.terminated_at is not None:
        raise SftpError("Storage is terminated", 409)
    user = require_user(tenant, user_id)
    tenant.sftp_users.remove(user)
    db.delete(user)
    db.commit()
    db.refresh(tenant)


# --- views ---


def node_offers_sftp(node: Optional[StorageNode]) -> bool:
    return bool(node is not None and node.enabled and node.sftp_enabled and node.sftp_host)


def _iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    value = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def user_out(tenant: StorageTenant, user: StorageSftpUser) -> dict:
    endpoint = endpoint_for_tenant(tenant)
    login = sftp_login(tenant, user)
    command = f"sftp -P {endpoint['port']} {login}@{endpoint['host']}" if endpoint else None
    return {
        "id": user.id,
        "name": user.name,
        "login": login,
        "public_keys": [
            {"key": k.get("key"), "type": k.get("type"), "fingerprint": k.get("fingerprint"), "comment": k.get("comment")}
            for k in (user.public_keys or [])
        ],
        "has_password": bool(user.password_hash),
        "allowed_ips": list(user.allowed_ips or []),
        "enabled": bool(user.enabled),
        "last_login_at": _iso(user.last_login_at),
        "last_login_ip": user.last_login_ip,
        "sftp_command": command,
    }


def tenant_sftp_out(tenant: StorageTenant, storage_status: str, *, staff: bool) -> dict:
    """The storage's SFTP endpoint, state and users."""
    endpoint = endpoint_for_tenant(tenant)
    available = endpoint is not None
    shares = [
        {"name": share.name, "access": share.sftp_access.value, "path": f"/{share.name}"}
        for share in tenant.shares
        if share.state.value == "active" and share.sftp_access != SftpAccess.OFF
    ]
    if not available:
        state = "unavailable"
    elif storage_status in ("suspended", "terminated"):
        state = storage_status
    elif not shares or not any(user.enabled for user in tenant.sftp_users):
        state = "off"
    else:
        state = tenant.sftp_state if tenant.sftp_state in ("ready", "error") else "provisioning"
    out = {
        "available": available,
        "state": state,
        "host": endpoint["host"] if available else None,
        "port": endpoint["port"] if available else None,
        "host_key_fingerprint": endpoint["fingerprint"] if available else None,
        "shares": shares,
        "users": [user_out(tenant, user) for user in tenant.sftp_users],
    }
    if staff:
        out["runner_state"] = tenant.sftp_state
        out["runner_error"] = tenant.sftp_error
    return out


# --- runner ---


def runner_settings(node: StorageNode) -> dict:
    return {
        "enabled": bool(node.sftp_enabled),
        "port": int(node.sftp_port or 2222),
        "max_auth_failures": int(settings.storage_sftp_max_auth_failures),
        "failure_window_seconds": int(settings.storage_sftp_failure_window_seconds),
        "ban_seconds": int(settings.storage_sftp_ban_seconds),
    }


def runner_users(tenant: StorageTenant, tenant_state: str, node: StorageNode) -> List[dict]:
    """Logins served for a tenant: only while it is active on a node with SFTP on."""
    # Logins carry the service ID, so a tenant without one must not serve any.
    if tenant_state != "active" or not node.sftp_enabled or tenant.service_id is None:
        return []
    out = []
    for user in tenant.sftp_users:
        keys = [k["key"] for k in (user.public_keys or []) if k.get("key")]
        if not user.enabled or (not keys and not user.password_hash):
            continue
        out.append(
            {
                "id": user.id,
                "username": sftp_login(tenant, user),
                "public_keys": keys,
                "password_hash": user.password_hash,
                "allowed_ips": list(user.allowed_ips or []),
            }
        )
    return out


def _short(value, limit: int) -> Optional[str]:
    return None if value is None else str(value)[:limit]


def _parse_time(value) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def apply_node_status(node: StorageNode, report: dict) -> None:
    info = report.get("sftp")
    if not isinstance(info, dict):
        node.sftp_listening = None
        node.sftp_error = None
        return
    listening = info.get("listening")
    node.sftp_listening = listening if isinstance(listening, bool) else None
    node.sftp_host_key = _short(info.get("host_key"), 2000) or node.sftp_host_key
    node.sftp_host_key_fingerprint = _short(info.get("host_key_fingerprint"), 128) or node.sftp_host_key_fingerprint
    node.sftp_error = _short(info.get("error"), 2000)


def apply_tenant_status(tenant: StorageTenant, entry: dict) -> None:
    info = entry.get("sftp")
    if not isinstance(info, dict):
        return
    if info.get("state") in SFTP_RUNNER_STATES:
        tenant.sftp_state = info["state"]
    tenant.sftp_error = _short(info.get("error"), 2000)


def apply_logins(tenants: dict, report: dict) -> None:
    """Record the latest successful login per user; users on other nodes are ignored."""
    users = {user.id: user for tenant in tenants.values() for user in tenant.sftp_users}
    for entry in (report.get("sftp_logins") or [])[:1000]:
        if not isinstance(entry, dict):
            continue
        user = users.get(entry.get("user_id"))
        at = _parse_time(entry.get("at"))
        if user is None or at is None:
            continue
        current = user.last_login_at
        if current is not None and current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        if current is None or at > current:
            user.last_login_at = at
            ip = entry.get("ip")
            try:
                user.last_login_ip = str(ipaddress.ip_address(str(ip))) if ip else None
            except ValueError:
                user.last_login_ip = None
            session = object_session(user)
            if session is not None:
                from app.services.client_notifications import on_sftp_login

                on_sftp_login(session, user.tenant, user, user.last_login_ip, at)
