"""Plugin key-value state, plugin-wide or per service (docs/plugin-system.md section 8.3)."""

from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.plugin_host import PluginState
from app.plugin_host.manifest import STATE_KEY_RE

MAX_VALUE_BYTES = 64 * 1024
MAX_KEYS_PER_PLUGIN = 100_000
GLOBAL_SCOPE = "global"


class StateError(ValueError):
    pass


def service_scope(service_id: int) -> str:
    return f"service:{int(service_id)}"


def _check_key(key: str) -> str:
    if not isinstance(key, str) or not STATE_KEY_RE.match(key):
        raise StateError("state keys must match ^[A-Za-z0-9_.:-]{1,128}$")
    return key


def _check_value(value: Any) -> Any:
    try:
        text = json.dumps(value)
    except (TypeError, ValueError) as exc:
        raise StateError("state values must be JSON-serialisable") from exc
    if len(text.encode("utf-8")) > MAX_VALUE_BYTES:
        raise StateError("a state value is limited to 64 KB")
    return json.loads(text)


class StateView:
    def __init__(self, db: Session, plugin_id: str, *, service_id: Optional[int] = None) -> None:
        self._db = db
        self._plugin_id = plugin_id
        self._service_id = service_id
        self._scope = service_scope(service_id) if service_id is not None else GLOBAL_SCOPE

    def _row(self, key: str) -> Optional[PluginState]:
        return self._db.execute(
            select(PluginState).where(
                PluginState.plugin_id == self._plugin_id,
                PluginState.scope == self._scope,
                PluginState.key == key,
            )
        ).scalar_one_or_none()

    def get(self, key: str, default: Any = None) -> Any:
        row = self._row(_check_key(key))
        return default if row is None or row.value is None else row.value

    def set(self, key: str, value: Any) -> None:
        _check_key(key)
        value = _check_value(value)
        row = self._row(key)
        if row is None:
            count = self._db.execute(
                select(func.count(PluginState.id)).where(PluginState.plugin_id == self._plugin_id)
            ).scalar_one()
            if count >= MAX_KEYS_PER_PLUGIN:
                raise StateError("this plugin has reached its limit of 100,000 state keys")
            self._db.add(
                PluginState(
                    plugin_id=self._plugin_id,
                    scope=self._scope,
                    key=key,
                    service_id=self._service_id,
                    value=value,
                )
            )
        else:
            row.value = value
        self._db.flush()

    def delete(self, key: str) -> None:
        row = self._row(_check_key(key))
        if row is not None:
            self._db.delete(row)
            self._db.flush()

    def all(self) -> dict[str, Any]:
        return read_scope(self._db, self._plugin_id, self._scope)

    def keys(self, prefix: str = "") -> list[str]:
        return sorted(k for k in self.all() if k.startswith(prefix))


def read_scope(db: Session, plugin_id: str, scope: str) -> dict[str, Any]:
    rows = db.execute(
        select(PluginState.key, PluginState.value).where(PluginState.plugin_id == plugin_id, PluginState.scope == scope)
    ).all()
    return {key: value for key, value in rows}


def read_service_state(db: Session, plugin_id: str, service_id: int) -> dict[str, Any]:
    return read_scope(db, plugin_id, service_scope(service_id))
