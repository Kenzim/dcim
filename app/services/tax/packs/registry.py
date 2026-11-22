"""Registry of the built-in and plugin tax rule packs (docs/billing-phase2-design.md 7.1).

Built-in packs are the JSON files beside this module (source ``builtin``).
Enabled ``tax_rule_pack`` plugin providers are listed as
``<plugin_id>:<provider_id>`` (source ``plugin``); a provider that fails is
listed as unavailable and never breaks the listing.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.plugin_host import providers as plugin_providers
from app.plugin_host.providers import ProviderUnavailable
from app.services.tax.packs.schema import PackFormatError, PackSchema, expand_rules, validate_pack

logger = logging.getLogger(__name__)

PACK_DIR = Path(__file__).resolve().parent
BUILTIN_PACKS = ("uk_vat", "eu_vat")
PACK_KIND = "tax_rule_pack"


class PackNotFound(LookupError):
    """No pack with that id and version."""


class PackUnavailable(RuntimeError):
    """A plugin pack could not be loaded (provider failure or invalid data)."""


@dataclass(frozen=True)
class PackInfo:
    pack_id: str  # "uk_vat", "eu_vat" or "<plugin_id>:<provider_id>"
    version: str
    source: str  # "builtin" or "plugin"
    available: bool = True
    error: Optional[str] = None
    title: Optional[str] = None


@dataclass(frozen=True)
class PackData:
    pack_id: str
    version: str
    # Expanded rows with the ``tax_rules`` column names (``tax_class`` is the class code).
    rules: tuple[dict[str, Any], ...] = ()
    # One entry per (key, locale): {"key", "locale", "text"}.
    legal_notes: tuple[dict[str, Any], ...] = ()
    # "jurisdictions", "classes", "metadata" and "source".
    extra: dict[str, Any] = field(default_factory=dict)


def _pack_data(pack_id: str, schema: PackSchema, source: str) -> PackData:
    notes = tuple(
        {"key": key, "locale": locale, "text": text}
        for key in sorted(schema.legal_notes)
        for locale, text in sorted(schema.legal_notes[key].items())
    )
    return PackData(
        pack_id=pack_id,
        version=schema.version,
        rules=tuple(expand_rules(schema)),
        legal_notes=notes,
        extra={
            "jurisdictions": list(schema.jurisdictions),
            "classes": list(schema.classes),
            "metadata": dict(schema.metadata),
            "source": source,
        },
    )


@lru_cache(maxsize=None)
def _builtin(pack_id: str) -> PackData:
    path = PACK_DIR / f"{pack_id}.json"
    with path.open(encoding="utf-8") as handle:
        schema = validate_pack(json.load(handle))
    if schema.pack_id != pack_id:
        raise PackFormatError(f"{path.name} declares pack_id {schema.pack_id!r}")
    return _pack_data(pack_id, schema, "builtin")


def _plugin_refs() -> list[str]:
    try:
        return sorted(ref.ref for ref in plugin_providers.providers_of(PACK_KIND))
    except Exception:  # noqa: BLE001 - listing packs never fails because of the plugin host
        logger.exception("Listing tax rule pack providers failed")
        return []


def _plugin_versions(ref: str) -> list[str]:
    versions = plugin_providers.call_provider(PACK_KIND, ref, "versions")
    if not isinstance(versions, (list, tuple)) or not all(isinstance(v, str) and v for v in versions):
        raise PackUnavailable("versions() did not return a list of strings")
    return list(versions)


class PackRegistry:
    @staticmethod
    def list(db: Session) -> list[PackInfo]:
        """Every pack with its newest version: the built-in ones first, then plugin packs by ref."""
        infos = []
        for pack_id in BUILTIN_PACKS:
            data = _builtin(pack_id)
            infos.append(
                PackInfo(
                    pack_id=pack_id,
                    version=data.version,
                    source="builtin",
                    title=data.extra["metadata"].get("title"),
                )
            )
        for ref in _plugin_refs():
            try:
                versions = _plugin_versions(ref)
                if not versions:
                    raise PackUnavailable("the provider offers no version")
                infos.append(PackInfo(pack_id=ref, version=versions[-1], source="plugin"))
            except (ProviderUnavailable, PackUnavailable) as exc:
                reason = getattr(exc, "reason", None) or str(exc)
                infos.append(PackInfo(pack_id=ref, version="", source="plugin", available=False, error=reason))
        return infos

    @staticmethod
    def load(db: Session, pack_id: str, version: str) -> PackData:
        """The validated, expanded pack. Raises ``PackNotFound`` or ``PackUnavailable``."""
        if pack_id in BUILTIN_PACKS:
            data = _builtin(pack_id)
            if data.version != version:
                raise PackNotFound(f"pack {pack_id} has version {data.version}, not {version}")
            return data
        if pack_id not in _plugin_refs():
            raise PackNotFound(f"no pack {pack_id}")
        try:
            if version not in _plugin_versions(pack_id):
                raise PackNotFound(f"pack {pack_id} has no version {version}")
            raw = plugin_providers.call_provider(PACK_KIND, pack_id, "load", version)
        except ProviderUnavailable as exc:
            raise PackUnavailable(exc.reason) from exc
        try:
            schema = validate_pack(raw)
        except PackFormatError as exc:
            raise PackUnavailable(f"invalid pack data: {exc}") from exc
        if schema.version != version:
            raise PackUnavailable(f"load({version!r}) returned version {schema.version!r}")
        return _pack_data(pack_id, schema, "plugin")


# Section 7 of the design calls the registry ``TaxRulePackRegistry``.
TaxRulePackRegistry = PackRegistry
