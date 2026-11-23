"""Proxmox VM firewall: per-service state, ordered rules, security groups, IP sets and baselines.

See docs/vm-firewall.md section 4. Every table is new; the "exactly one owner" check on
``firewall_rules`` exists only on MariaDB (created by the migration), so the service layer
enforces it everywhere.
"""

import enum

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.core.database import Base


class FirewallBackend(str, enum.Enum):
    PROXMOX_VM = "proxmox_vm"


class FirewallPolicy(str, enum.Enum):
    DROP = "DROP"
    REJECT = "REJECT"
    ACCEPT = "ACCEPT"


class FirewallLogLevel(str, enum.Enum):
    """Proxmox log levels (``pve-fw-loglevel``)."""

    NOLOG = "nolog"
    EMERG = "emerg"
    ALERT = "alert"
    CRIT = "crit"
    ERR = "err"
    WARNING = "warning"
    NOTICE = "notice"
    INFO = "info"
    DEBUG = "debug"


class FirewallNicFlag(str, enum.Enum):
    UNKNOWN = "unknown"
    OFF = "off"
    ON = "on"
    PENDING_REBOOT = "pending_reboot"


class FirewallState(str, enum.Enum):
    OFF = "off"
    PENDING = "pending"
    APPLIED = "applied"
    PENDING_REBOOT = "pending_reboot"
    ERROR = "error"
    DRIFT = "drift"
    NOT_ENFORCED = "not_enforced"
    UNMANAGED = "unmanaged"
    VM_MISSING = "vm_missing"


class FirewallRuleKind(str, enum.Enum):
    RULE = "rule"
    GROUP = "group"


class FirewallDirection(str, enum.Enum):
    IN = "in"
    OUT = "out"


class FirewallAction(str, enum.Enum):
    ACCEPT = "ACCEPT"
    DROP = "DROP"
    REJECT = "REJECT"


class FirewallIpVersion(str, enum.Enum):
    ANY = "any"
    V4 = "4"
    V6 = "6"


class FirewallObjectScope(str, enum.Enum):
    CLIENT = "client"
    ADMIN = "admin"


class FirewallIpsetPurpose(str, enum.Enum):
    GENERAL = "general"
    MANAGEMENT = "management"


class FirewallBaselineScope(str, enum.Enum):
    GLOBAL = "global"
    LOCATION = "location"
    PRODUCT = "product"


def _enum(enum_cls, length: int = 16):
    return SQLEnum(enum_cls, native_enum=False, length=length, values_callable=lambda x: [e.value for e in x])


def _created_at():
    return Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _updated_at():
    return Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class ServiceFirewall(Base):
    """Firewall settings and reconcile state of one managed service."""

    __tablename__ = "service_firewalls"

    service_id = Column(Integer, ForeignKey("services.id", ondelete="CASCADE"), primary_key=True)
    backend = Column(String(32), nullable=False, default=FirewallBackend.PROXMOX_VM.value)
    client_enabled = Column(Boolean, nullable=False, default=False)
    policy_in = Column(_enum(FirewallPolicy), nullable=False, default=FirewallPolicy.DROP)
    policy_out = Column(_enum(FirewallPolicy), nullable=False, default=FirewallPolicy.ACCEPT)
    log_level_in = Column(_enum(FirewallLogLevel), nullable=False, default=FirewallLogLevel.NOLOG)
    log_level_out = Column(_enum(FirewallLogLevel), nullable=False, default=FirewallLogLevel.NOLOG)
    managed = Column(Boolean, nullable=False, default=True)
    # Baseline enforced while ``client_enabled`` is false (set for new VMs, section 4.5a).
    enforce_baseline = Column(Boolean, nullable=False, default=False)
    excluded = Column(Boolean, nullable=False, default=False)
    admin_overrides = Column(JSON, nullable=True)
    # Bridge of net0 as last read from the Proxmox VM config by eligibility or apply; null until
    # observed. Render uses it for the IPv6 router-advert bridge rule (section 3.1).
    net0_bridge = Column(String(32), nullable=True)
    # Staged rollout of the location's ``enforce_baseline_on_all`` (section 8.3): render ignores
    # that switch for this row until this time; ``release_due_rollouts`` clears it when due.
    rollout_after = Column(DateTime(timezone=True), nullable=True)
    # Set when ``release_due_rollouts`` releases the row; the worker counts the outcome of its next
    # apply towards the location's rollout pause and clears it.
    rollout_released_at = Column(DateTime(timezone=True), nullable=True)
    revision = Column(Integer, nullable=False, default=1)
    applied_revision = Column(Integer, nullable=True)
    desired_hash = Column(String(64), nullable=True)
    applied_hash = Column(String(64), nullable=True)
    applied_digest = Column(String(64), nullable=True)
    nic_flag = Column(_enum(FirewallNicFlag), nullable=False, default=FirewallNicFlag.UNKNOWN)
    state = Column(_enum(FirewallState), nullable=False, default=FirewallState.OFF, index=True)
    last_error = Column(Text, nullable=True)
    failing_since = Column(DateTime(timezone=True), nullable=True)
    last_applied_at = Column(DateTime(timezone=True), nullable=True)
    last_checked_at = Column(DateTime(timezone=True), nullable=True)
    next_attempt_at = Column(DateTime(timezone=True), nullable=True)
    claimed_by = Column(String(128), nullable=True)
    claimed_until = Column(DateTime(timezone=True), nullable=True)
    drift_repairs_24h = Column(Integer, nullable=False, default=0)
    created_at = _created_at()
    updated_at = _updated_at()

    service = relationship("Service")
    rules = relationship(
        "FirewallRule",
        primaryjoin="ServiceFirewall.service_id == FirewallRule.service_id",
        order_by="FirewallRule.position",
        cascade="all, delete-orphan",
        passive_deletes=True,
        overlaps="rules",
    )


class FirewallGroup(Base):
    """A named, ordered rule list a VM or a baseline can reference (security group)."""

    __tablename__ = "firewall_groups"
    __table_args__ = (UniqueConstraint("owner_user_id", "name", name="uq_firewall_groups_owner_name"),)

    id = Column(Integer, primary_key=True)
    name = Column(String(48), nullable=False)
    description = Column(String(255), nullable=True)
    scope = Column(_enum(FirewallObjectScope), nullable=False)
    owner_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    client_selectable = Column(Boolean, nullable=False, default=False)
    revision = Column(Integer, nullable=False, default=1)
    created_at = _created_at()
    updated_at = _updated_at()

    rules = relationship(
        "FirewallRule",
        primaryjoin="FirewallGroup.id == FirewallRule.group_id",
        order_by="FirewallRule.position",
        cascade="all, delete-orphan",
        passive_deletes=True,
        overlaps="rules",
    )


class FirewallIpset(Base):
    """A named list of networks, copied into each VM that references it as ``rf-s<id>``."""

    __tablename__ = "firewall_ipsets"
    __table_args__ = (UniqueConstraint("owner_user_id", "name", name="uq_firewall_ipsets_owner_name"),)

    id = Column(Integer, primary_key=True)
    name = Column(String(48), nullable=False)
    description = Column(String(255), nullable=True)
    scope = Column(_enum(FirewallObjectScope), nullable=False)
    owner_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    client_selectable = Column(Boolean, nullable=False, default=False)
    purpose = Column(_enum(FirewallIpsetPurpose), nullable=False, default=FirewallIpsetPurpose.GENERAL)
    revision = Column(Integer, nullable=False, default=1)
    created_at = _created_at()
    updated_at = _updated_at()

    entries = relationship(
        "FirewallIpsetEntry",
        order_by="FirewallIpsetEntry.id",
        cascade="all, delete-orphan",
        passive_deletes=True,
        back_populates="ipset",
    )


class FirewallIpsetEntry(Base):
    __tablename__ = "firewall_ipset_entries"
    __table_args__ = (UniqueConstraint("ipset_id", "cidr", name="uq_firewall_ipset_entries_ipset_cidr"),)

    id = Column(Integer, primary_key=True)
    ipset_id = Column(Integer, ForeignKey("firewall_ipsets.id", ondelete="CASCADE"), nullable=False, index=True)
    cidr = Column(String(49), nullable=False)
    nomatch = Column(Boolean, nullable=False, default=False)
    comment = Column(String(120), nullable=True)

    ipset = relationship("FirewallIpset", back_populates="entries")


class FirewallBaseline(Base):
    """Admin protections for every VM in a scope: settings plus rules evaluated before client rules."""

    __tablename__ = "firewall_baselines"
    __table_args__ = (UniqueConstraint("scope_type", "scope_id", name="uq_firewall_baselines_scope"),)

    id = Column(Integer, primary_key=True)
    name = Column(String(64), nullable=False)
    scope_type = Column(_enum(FirewallBaselineScope), nullable=False)
    scope_id = Column(Integer, nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
    settings = Column(JSON, nullable=True)
    revision = Column(Integer, nullable=False, default=1)
    created_at = _created_at()
    updated_at = _updated_at()

    rules = relationship(
        "FirewallRule",
        primaryjoin="FirewallBaseline.id == FirewallRule.baseline_id",
        order_by="FirewallRule.position",
        cascade="all, delete-orphan",
        passive_deletes=True,
        overlaps="rules",
    )


class FirewallRule(Base):
    """One entry of an ordered rule list owned by a VM, a group or a baseline."""

    __tablename__ = "firewall_rules"
    __table_args__ = (
        Index("ix_firewall_rules_service_position", "service_id", "position"),
        Index("ix_firewall_rules_group_position", "group_id", "position"),
        Index("ix_firewall_rules_baseline_position", "baseline_id", "position"),
    )

    id = Column(Integer, primary_key=True)
    service_id = Column(Integer, ForeignKey("service_firewalls.service_id", ondelete="CASCADE"), nullable=True)
    group_id = Column(Integer, ForeignKey("firewall_groups.id", ondelete="CASCADE"), nullable=True)
    baseline_id = Column(Integer, ForeignKey("firewall_baselines.id", ondelete="CASCADE"), nullable=True)
    position = Column(Integer, nullable=False)
    kind = Column(_enum(FirewallRuleKind), nullable=False, default=FirewallRuleKind.RULE)
    ref_group_id = Column(Integer, ForeignKey("firewall_groups.id"), nullable=True, index=True)
    direction = Column(_enum(FirewallDirection), nullable=True)
    action = Column(_enum(FirewallAction), nullable=True)
    ip_version = Column(_enum(FirewallIpVersion), nullable=False, default=FirewallIpVersion.ANY)
    protocol = Column(String(16), nullable=True)
    ports = Column(String(128), nullable=True)
    source_ports = Column(String(128), nullable=True)
    icmp_type = Column(String(32), nullable=True)
    remote_cidrs = Column(JSON, nullable=True)
    remote_ipset_id = Column(Integer, ForeignKey("firewall_ipsets.id"), nullable=True, index=True)
    local_cidrs = Column(JSON, nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
    log = Column(Boolean, nullable=False, default=False)
    comment = Column(String(120), nullable=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = _created_at()
    updated_at = _updated_at()


class FirewallLocationSettings(Base):
    """Per-location opt-in and the cached readiness of its clusters."""

    __tablename__ = "firewall_location_settings"

    location_id = Column(Integer, ForeignKey("locations.id", ondelete="CASCADE"), primary_key=True)
    enabled = Column(Boolean, nullable=False, default=False)
    nic_flag_on_provision = Column(Boolean, nullable=False, default=True)
    enforce_baseline_on_all = Column(Boolean, nullable=False, default=False)
    # New VMs here get ``admin_overrides.smtp_out = "block"`` and an enforced baseline.
    smtp_block_new_vms = Column(Boolean, nullable=False, default=True)
    # Whether that enforced baseline also turns on ``ipfilter`` and ``macfilter``.
    anti_spoof_new_vms = Column(Boolean, nullable=False, default=False)
    # IPv6 prefixes added to ``ipfilter-net0`` only for VMs whose observed ``net0_bridge`` is in
    # ``vm_firewall_ipv6_ra_bridges``; such VMs get no anti-spoofing until this is set.
    # Admin help text: ``service.IPV6_ALLOWED_PREFIXES_HELP``.
    ipv6_allowed_prefixes = Column(JSON, nullable=True)
    readiness = Column(JSON, nullable=True)
    readiness_checked_at = Column(DateTime(timezone=True), nullable=True)
    # Staged rollout (section 8.3): consecutive failed applies among released rows; at 3 the worker
    # pauses the rollout (no more rows are released) until staff resume it.
    rollout_failures = Column(Integer, nullable=False, default=0)
    rollout_paused_at = Column(DateTime(timezone=True), nullable=True)
    rollout_pause_reason = Column(Text, nullable=True)
    created_at = _created_at()
    updated_at = _updated_at()
