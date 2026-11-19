"""Client notifications: opt-in notices about a client's own account and services.

Everything here is scoped to one user. A notice is addressed to the user the
event belongs to, built from a fixed sentence plus an allow-list of facts the
client can already see (their own IP address and user agent, the names of their
own services and shares). Server, node, host, runner and switch names, other
clients' data, staff names and internal error text never enter a notice.

Hooks are fail-open: a problem here never breaks the action that caused it.
"""

from __future__ import annotations

import logging
import html
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import event, func, select
from sqlalchemy.orm import Session
from sqlalchemy.orm import attributes as orm_attributes

from app.core.config import settings
from app.models.alerting import (
    Alert,
    AlertAudience,
    ChannelKind,
    ClientNotificationPref,
    DeliveryStatus,
    NotificationDelivery,
)
from app.models.commerce_audit import UserAuditEvent
from app.models.service import Service, ServiceStatus
from app.models.user import User
from app.services.alerting import safe_http
from app.services.alerting.catalogue import get_global_settings, naive, utcnow
from app.services.alerting.dispatch import _valid_email
from app.services.alerting.secrets import mask_url, new_signing_secret, seal, unseal

logger = logging.getLogger(__name__)

SFTP_LOGIN_MAX_AGE = timedelta(minutes=15)


@dataclass(frozen=True)
class Category:
    key: str
    label: str
    description: str


CATEGORY_LIST = (
    Category("sign_in", "Sign-ins", "A successful sign-in to your account."),
    Category("failed_sign_in", "Failed sign-ins", "A failed sign-in or a wrong two-factor code on your account."),
    Category("two_factor", "Two-factor changes", "Two-factor authentication turned on or off, or recovery codes used or renewed."),
    Category("password", "Password changes", "A password reset requested or completed, or a password changed."),
    Category("mcp_keys", "MCP key changes", "An MCP key created, changed or deleted."),
    Category("sftp_login", "SFTP sign-ins", "A sign-in to the SFTP access of your storage."),
    Category("service_suspended", "Service suspended", "One of your services is suspended."),
    Category("service_terminated", "Service terminated", "One of your services is terminated."),
    Category("storage_quota", "Storage nearly full", "A storage share of yours is close to its quota."),
    Category("backup", "Backups", "A backup of one of your services finished or failed."),
    Category("reinstall", "Installs and reinstalls", "An install or reinstall of one of your services finished or failed."),
)
CATEGORIES = {c.key: c for c in CATEGORY_LIST}

_AUDIT_MAP: dict[str, tuple[str, str]] = {
    "login_success": ("sign_in", "A sign-in to your account succeeded."),
    "login_failed": ("failed_sign_in", "A sign-in to your account failed."),
    "login_2fa_failed": ("failed_sign_in", "A wrong two-factor code was entered for your account."),
    "2fa_enabled": ("two_factor", "Two-factor authentication was turned on for your account."),
    "2fa_disabled": ("two_factor", "Two-factor authentication was turned off for your account."),
    "recovery_codes_regenerated": ("two_factor", "Your two-factor recovery codes were renewed."),
    "recovery_code_used": ("two_factor", "A two-factor recovery code was used to sign in to your account."),
    "require_2fa_changed": ("two_factor", "The two-factor requirement for your account was changed."),
    "password_reset_requested": ("password", "A password reset was requested for your account."),
    "password_reset_completed": ("password", "The password of your account was reset."),
    "password_set_by_admin": ("password", "The password of your account was changed."),
    "password_changed": ("password", "The password of your account was changed."),
}
_MCP_KEY_TEXT = {
    "create": "An MCP key was created on your account.",
    "update": "An MCP key on your account was changed.",
    "delete": "An MCP key on your account was deleted.",
    "admin_update": "An MCP key on your account was changed.",
    "admin_delete": "An MCP key on your account was deleted.",
}
_ALLOWED_DATA = ("service_id", "service_name", "share_name", "percent", "ip", "user_agent", "sftp_user")


# ------------------------------------------------------------------ preferences


def _prefs_row(db: Session, user_id: int) -> Optional[ClientNotificationPref]:
    return db.query(ClientNotificationPref).filter(ClientNotificationPref.user_id == user_id).first()


def _categories_of(row: Optional[ClientNotificationPref]) -> dict[str, bool]:
    stored = row.categories if row is not None and isinstance(row.categories, dict) else {}
    return {key: bool(stored.get(key, False)) for key in CATEGORIES}


def get_preferences(db: Session, user_id: int) -> dict[str, Any]:
    row = _prefs_row(db, user_id)
    url = unseal(row.webhook_url) if row is not None else None
    return {
        "email_enabled": True if row is None else bool(row.email_enabled),
        "categories": _categories_of(row),
        "catalogue": [{"key": c.key, "label": c.label, "description": c.description} for c in CATEGORY_LIST],
        "webhook": {
            "configured": bool(url),
            "url": mask_url(url),
            "error": row.webhook_error if row is not None else None,
        },
    }


class PreferenceError(ValueError):
    """The request is invalid; the message is safe to show to the client."""


def update_preferences(
    db: Session,
    user_id: int,
    *,
    categories: Optional[dict[str, Any]] = None,
    email_enabled: Optional[bool] = None,
    webhook_url: Optional[str] = None,
    regenerate_secret: bool = False,
    resolver: Optional[safe_http.Resolver] = None,
) -> dict[str, Any]:
    """Apply a change. A new webhook secret appears in the result once and is never readable again."""
    if categories is not None:
        unknown = [k for k in categories if k not in CATEGORIES]
        if unknown:
            raise PreferenceError(f"Unknown notification category: {unknown[0]}")
        if any(not isinstance(v, bool) for v in categories.values()):
            raise PreferenceError("Each category must be true or false")
    url = (webhook_url or "").strip() if webhook_url is not None else None
    if url:
        try:
            safe_http.validate_url(url, public_only=True, https_only=True, resolver=resolver)
        except safe_http.WebhookUrlError as exc:
            raise PreferenceError(str(exc)) from exc
    row = _prefs_row(db, user_id)
    if row is None:
        row = ClientNotificationPref(user_id=user_id, email_enabled=True, categories={})
        db.add(row)
    if categories is not None:
        merged = _categories_of(row)
        merged.update(categories)
        row.categories = merged
    if email_enabled is not None:
        row.email_enabled = bool(email_enabled)
    new_secret: Optional[str] = None
    if url is not None:
        if not url:
            row.webhook_url = None
            row.webhook_secret = None
            row.webhook_error = None
        else:
            row.webhook_url = seal(url)
            row.webhook_error = None
            if not row.webhook_secret:
                regenerate_secret = True
    if regenerate_secret and (row.webhook_url or url):
        new_secret = new_signing_secret()
        row.webhook_secret = seal(new_secret)
    db.commit()
    result = get_preferences(db, user_id)
    if new_secret:
        result["webhook"]["secret"] = new_secret
    return result


# ------------------------------------------------------------------ core delivery


def _within_rate(db: Session, user_id: int, kind: str, now: datetime, limit: int) -> bool:
    since = now - timedelta(hours=1)
    count = (
        db.query(func.count(NotificationDelivery.id))
        .filter(
            NotificationDelivery.audience == AlertAudience.CLIENT,
            NotificationDelivery.user_id == user_id,
            NotificationDelivery.channel_kind == kind,
            NotificationDelivery.created_at >= since,
        )
        .scalar()
        or 0
    )
    return count < limit


def _email_payload(message: str, when: datetime, data: dict[str, Any], category: Category) -> dict[str, str]:
    lines = [message, "", f"Time: {naive(when).strftime('%Y-%m-%d %H:%M:%S')} UTC"]
    if data.get("ip"):
        lines.append(f"IP address: {data['ip']}")
    if data.get("user_agent"):
        lines.append(f"Browser: {data['user_agent']}")
    lines += [
        "",
        f"You receive this because you turned on \"{category.label}\" notifications. "
        "You can change this under Account, Notifications in the portal.",
    ]
    base = settings.public_base_url or settings.public_app_url
    if base:
        lines.append(base.rstrip("/"))
    text = "\n".join(lines)
    return {
        "subject": f"[Rackflow] {message}"[:200],
        "text": text,
        "html": "".join(f"<p>{html.escape(line)}</p>" if line else "" for line in lines),
    }


def notify(
    db: Session,
    user_id: Optional[int],
    category: str,
    message: str,
    *,
    event_key: str,
    data: Optional[dict[str, Any]] = None,
    when: Optional[datetime] = None,
    force: bool = False,
    flush: bool = True,
) -> int:
    """Queue a notice for one user if they opted in. Returns the rows queued. Never commits."""
    if user_id is None or category not in CATEGORIES:
        return 0
    row = _prefs_row(db, user_id)
    if row is None and not force:
        return 0
    if not force and not _categories_of(row).get(category):
        return 0
    user = db.get(User, user_id)
    if user is None:
        return 0
    now = utcnow()
    when = naive(when) or now
    safe = {k: v for k, v in (data or {}).items() if k in _ALLOWED_DATA and v not in (None, "")}
    limit = get_global_settings(db)["client_rate_limit_per_hour"]
    cat = CATEGORIES[category]
    new_rows: list[NotificationDelivery] = []

    email_on = True if row is None else bool(row.email_enabled)
    if email_on and _valid_email(user.email):
        if _within_rate(db, user_id, ChannelKind.EMAIL, now, limit):
            new_rows.append(
                NotificationDelivery(
                    idempotency_key=f"c:{event_key}:{category}:email"[:255],
                    audience=AlertAudience.CLIENT,
                    user_id=user_id,
                    channel_kind=ChannelKind.EMAIL,
                    target=user.email,
                    event=category,
                    payload=_email_payload(message, when, safe, cat),
                    status=DeliveryStatus.QUEUED,
                    next_attempt_at=now,
                )
            )
        else:
            logger.info("Client notification rate limit reached for user %s (email)", user_id)
    if row is not None and row.webhook_url:
        if _within_rate(db, user_id, ChannelKind.WEBHOOK, now, limit):
            body = {
                "version": 1,
                "event": "notification",
                "category": category,
                "message": message,
                "occurred_at": naive(when).isoformat() + "Z",
                "sent_at": now.isoformat() + "Z",
                "data": safe,
            }
            new_rows.append(
                NotificationDelivery(
                    idempotency_key=f"c:{event_key}:{category}:webhook"[:255],
                    audience=AlertAudience.CLIENT,
                    user_id=user_id,
                    channel_kind=ChannelKind.WEBHOOK,
                    target="webhook",
                    event=category,
                    payload=body,
                    status=DeliveryStatus.QUEUED,
                    next_attempt_at=now,
                )
            )
        else:
            logger.info("Client notification rate limit reached for user %s (webhook)", user_id)
    if not new_rows:
        return 0
    # The same event can be reported twice (an installer posting completion again, for
    # example). Skip notices already queued, and flush in a savepoint so a race on the
    # unique key never leaves the caller's session needing a rollback.
    existing = {
        key
        for (key,) in db.query(NotificationDelivery.idempotency_key).filter(
            NotificationDelivery.idempotency_key.in_([r.idempotency_key for r in new_rows])
        )
    }
    new_rows = [r for r in new_rows if r.idempotency_key not in existing]
    if not new_rows:
        return 0
    if flush:
        with db.begin_nested():
            db.add_all(new_rows)
            db.flush()
    else:
        db.add_all(new_rows)
    return len(new_rows)


def send_test(db: Session, user_id: int) -> int:
    """Queue a sample notice to the client's own channels, whatever categories they chose."""
    stamp = utcnow().isoformat()
    queued = notify(
        db,
        user_id,
        "sign_in",
        "This is a test notification from Rackflow. No action is needed.",
        event_key=f"test:{user_id}:{stamp}",
        force=True,
    )
    db.commit()
    return queued


def _safe(fn):
    """Run a hook; log and swallow any error so the caller is never affected."""

    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:  # noqa: BLE001 - fail-open
            logger.warning("Client notification hook %s failed", fn.__name__, exc_info=True)
            return 0

    wrapper.__name__ = fn.__name__
    return wrapper


# ------------------------------------------------------------------ hooks


@_safe
def on_audit_event(db: Session, row: UserAuditEvent) -> int:
    """Called by ``AuditService.log`` after the row is flushed."""
    user_id = row.subject_user_id
    if user_id is None:
        return 0
    action = row.action or ""
    if action.startswith("client_mcp_key."):
        category = "mcp_keys"
        message = _MCP_KEY_TEXT.get(action.split(".", 1)[1])
        if message is None:
            return 0
    else:
        mapped = _AUDIT_MAP.get(action)
        if mapped is None:
            return 0
        category, message = mapped
    with db.begin_nested():
        # The address and browser belong to whoever made the request. When staff acted on the
        # account they are the staff member's, so only the client's own actions carry them.
        own_action = row.actor_user_id == user_id
        return notify(
            db,
            user_id,
            category,
            message,
            event_key=f"audit:{row.id}",
            data={"ip": row.ip, "user_agent": row.user_agent} if own_action else None,
            when=utcnow(),
        )


@_safe
def on_sftp_login(db: Session, tenant: Any, sftp_user: Any, ip: Optional[str], at: datetime) -> int:
    service = tenant.service
    if service is None or service.owner_user_id is None:
        return 0
    if utcnow() - naive(at) > SFTP_LOGIN_MAX_AGE:
        return 0
    return notify(
        db,
        service.owner_user_id,
        "sftp_login",
        f"Someone signed in to the SFTP access of your storage {service.name}.",
        event_key=f"sftp:{sftp_user.id}:{naive(at).isoformat()}",
        data={"service_id": service.id, "service_name": service.name, "ip": ip, "sftp_user": sftp_user.name},
        when=at,
    )


def _service_event(db: Session, service: Service, category: str, message: str, key: str, when=None) -> int:
    return notify(
        db,
        service.owner_user_id,
        category,
        message,
        event_key=key,
        data={"service_id": service.id, "service_name": service.name},
        when=when,
    )


@_safe
def notify_backup_finished(db: Session, run: Any) -> int:
    service = db.get(Service, run.service_id)
    if service is None:
        return 0
    ok = run.status == "succeeded"
    message = (
        f"The backup of your service {service.name} completed."
        if ok
        else f"The backup of your service {service.name} failed. Check the service in the portal or contact support."
    )
    queued = _service_event(db, service, "backup", message, f"backup:{run.id}", run.finished_at)
    if queued:
        db.commit()
    return queued


@_safe
def notify_job_finished(db: Session, job: Any, ok: bool) -> int:
    """VM provisioning and reinstall jobs only; restores and password applies are not installs."""
    name = job.strategy_name or ""
    if name.startswith("restore") or name == "apply_guest_password":
        return 0
    service = job.service
    if service is None:
        return 0
    message = (
        f"The installation of your service {service.name} finished."
        if ok
        else f"The installation of your service {service.name} failed. Check the service in the portal or contact support."
    )
    return _service_event(db, service, "reinstall", message, f"job:{job.id}", job.finished_at)


@_safe
def notify_install_finished(db: Session, task: Any, ok: bool) -> int:
    from app.dao.service_dao import ServiceDAO

    queued = 0
    for service in ServiceDAO.get_by_server(db, task.server_id):
        if service.status == ServiceStatus.TERMINATED:
            continue
        message = (
            f"The installation of your service {service.name} finished."
            if ok
            else f"The installation of your service {service.name} failed. Check the service in the portal or contact support."
        )
        queued += _service_event(db, service, "reinstall", message, f"install:{task.id}:{service.id}")
    if queued:
        db.commit()
    return queued


def queue_alert_notices(db: Session, notices: list[Any], now: datetime) -> int:
    """Client-audience alerts (for example storage near quota) become notices when they open."""
    queued = 0
    for notice in notices:
        alert: Alert = notice.alert
        if notice.event != "opened" or alert.owner_user_id is None:
            continue
        if alert.alert_type != "client_storage_quota":
            continue
        details = alert.details if isinstance(alert.details, dict) else {}
        message = (
            f"Your storage share {details.get('share_name', '')} on {details.get('service_name', '')} "
            f"is using {details.get('percent', '')}% of its quota."
        )
        queued += notify(
            db,
            alert.owner_user_id,
            "storage_quota",
            message,
            event_key=f"alert:{alert.id}:{notice.seq}",
            data={
                "service_id": details.get("service_id"),
                "service_name": details.get("service_name"),
                "share_name": details.get("share_name"),
                "percent": details.get("percent"),
            },
        )
    return queued


# ------------------------------------------------------------------ service status listener


_STATUS_CATEGORY = {
    ServiceStatus.SUSPENDED: ("service_suspended", "Your service {name} has been suspended."),
    ServiceStatus.TERMINATED: ("service_terminated", "Your service {name} has been terminated."),
}


def _on_before_flush(session: Session, flush_context: Any, instances: Any) -> None:
    try:
        changed = []
        for obj in list(session.dirty):
            if not isinstance(obj, Service) or obj.owner_user_id is None:
                continue
            history = orm_attributes.get_history(obj, "status")
            if not history.added:
                continue
            new = history.added[0]
            if new not in _STATUS_CATEGORY:
                continue
            if history.deleted:
                old = history.deleted[0]
            else:
                # The old value was not loaded (expired instance): read what is stored.
                with session.no_autoflush:
                    old = session.execute(select(Service.status).where(Service.id == obj.id)).scalar()
            if new != old:
                changed.append((obj, new))
        if not changed:
            return
        with session.no_autoflush:
            for service, new in changed:
                category, template = _STATUS_CATEGORY[new]
                notify(
                    session,
                    service.owner_user_id,
                    category,
                    template.format(name=service.name),
                    event_key=f"service:{service.id}:{new.value}:{utcnow().isoformat()}",
                    data={"service_id": service.id, "service_name": service.name},
                    flush=False,
                )
    except Exception:  # noqa: BLE001 - fail-open
        logger.warning("Service status notification failed", exc_info=True)


event.listen(Session, "before_flush", _on_before_flush)
