"""Validation of firewall rules, groups, IP sets and limits (docs/vm-firewall.md section 4.6).

Every function raises ``FirewallValidationError`` with a stable code and the field path of the
offending value. Inputs are plain mappings (what an API request model dumps); outputs are
normalised dataclasses the service layer stores as they are.
"""

from __future__ import annotations

import ipaddress
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence

from app.models.vm_firewall import (
    FirewallAction,
    FirewallDirection,
    FirewallIpVersion,
    FirewallLogLevel,
    FirewallObjectScope,
    FirewallPolicy,
    FirewallRuleKind,
)
from app.services.vm_firewall.errors import FirewallValidationError

PROTOCOLS = ("tcp", "udp", "icmp", "ipv6-icmp", "sctp", "gre", "esp", "ah")
PORT_PROTOCOLS = ("tcp", "udp", "sctp")
MAX_PORT_ITEMS = 10
# iptables multiport takes 15 slots and a range uses two.
MAX_PORT_SLOTS = 15
MAX_CIDRS_PER_RULE = 20
MAX_NAME_LENGTH = 48
MAX_DESCRIPTION_LENGTH = 255
MAX_COMMENT_LENGTH = 120

ICMP_TYPES = frozenset(
    {
        "any", "echo-reply", "destination-unreachable", "network-unreachable", "host-unreachable",
        "protocol-unreachable", "port-unreachable", "fragmentation-needed", "source-route-failed",
        "network-unknown", "host-unknown", "network-prohibited", "host-prohibited",
        "TOS-network-unreachable", "TOS-host-unreachable", "communication-prohibited",
        "host-precedence-violation", "precedence-cutoff", "source-quench", "redirect",
        "network-redirect", "host-redirect", "TOS-network-redirect", "TOS-host-redirect",
        "echo-request", "router-advertisement", "router-solicitation", "time-exceeded",
        "ttl-zero-during-transit", "ttl-zero-during-reassembly", "parameter-problem",
        "ip-header-bad", "required-option-missing", "timestamp-request", "timestamp-reply",
        "address-mask-request", "address-mask-reply",
    }
)
ICMPV6_TYPES = frozenset(
    {
        "destination-unreachable", "no-route", "communication-prohibited", "beyond-scope",
        "address-unreachable", "port-unreachable", "failed-policy", "reject-route", "packet-too-big",
        "time-exceeded", "ttl-zero-during-transit", "ttl-zero-during-reassembly", "parameter-problem",
        "bad-header", "unknown-header-type", "unknown-option", "echo-request", "echo-reply",
        "router-solicitation", "router-advertisement", "neighbour-solicitation",
        "neighbor-solicitation", "neighbour-advertisement", "neighbor-advertisement", "redirect",
    }
)

RULE_FIELDS = frozenset(
    {
        "kind", "group_id", "direction", "action", "ip_version", "protocol", "ports", "source_ports",
        "icmp_type", "remote_cidrs", "remote_ipset_id", "local_cidrs", "enabled", "log", "comment",
    }
)
# Read-only keys a caller may send back unchanged from a previous read.
IGNORED_RULE_FIELDS = frozenset({"id", "position"})
BASELINE_SETTING_KEYS = frozenset(
    {"anti_spoof", "macfilter", "smtp_out", "block_management", "management_ipset_ids", "client_may_disable"}
)
ADMIN_OVERRIDE_KEYS = frozenset({"anti_spoof", "macfilter", "smtp_out", "block_management", "extra_ipfilter"})
SMTP_OUT_VALUES = ("block", "allow")


@dataclass
class RuleSpec:
    """A validated rule entry; field names match ``FirewallRule`` columns."""

    kind: str = FirewallRuleKind.RULE.value
    ref_group_id: Optional[int] = None
    direction: Optional[str] = None
    action: Optional[str] = None
    ip_version: str = FirewallIpVersion.ANY.value
    protocol: Optional[str] = None
    ports: Optional[str] = None
    source_ports: Optional[str] = None
    icmp_type: Optional[str] = None
    remote_cidrs: Optional[list[str]] = None
    remote_ipset_id: Optional[int] = None
    local_cidrs: Optional[list[str]] = None
    enabled: bool = True
    log: bool = False
    comment: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def same_effect(self, other: "RuleSpec") -> bool:
        """Equal apart from the comment (used to keep presets idempotent)."""
        mine, theirs = self.as_dict(), other.as_dict()
        mine.pop("comment")
        theirs.pop("comment")
        return mine == theirs


@dataclass
class IpsetEntrySpec:
    cidr: str
    nomatch: bool = False
    comment: Optional[str] = None


@dataclass
class Limits:
    max_rules_per_vm: int = 100
    max_rendered_rules: int = 300
    max_groups_per_client: int = 20
    max_rules_per_group: int = 50
    max_groups_per_vm: int = 5
    max_ipsets_per_client: int = 20
    max_ipset_entries: int = 64
    client_logging: bool = False


def _fail(field_path: str, code: str, message: str):
    raise FirewallValidationError(message, code=code, field=field_path)


def _choice(value: Any, enum_cls, field_path: str, *, upper: bool = False) -> str:
    allowed = [e.value for e in enum_cls]
    text = value.value if isinstance(value, enum_cls) else value
    if isinstance(text, str) and upper:
        text = text.upper()
    if not isinstance(text, str) or text not in allowed:
        _fail(field_path, "invalid_value", "must be one of: " + ", ".join(allowed))
    return text


def validate_policy(value: Any, field_path: str = "policy_in") -> str:
    return _choice(value, FirewallPolicy, field_path, upper=True)


def validate_log_level(value: Any, field_path: str = "log_level_in") -> str:
    return _choice(value, FirewallLogLevel, field_path)


def _text(value: Any, field_path: str, max_length: int, *, required: bool) -> Optional[str]:
    if value is None:
        if required:
            _fail(field_path, "required", "is required")
        return None
    if not isinstance(value, str):
        _fail(field_path, "invalid_value", "must be text")
    text = value.strip()
    if not text:
        if required:
            _fail(field_path, "required", "is required")
        return None
    if len(text) > max_length:
        _fail(field_path, "too_long", f"must be at most {max_length} characters")
    if any(ord(ch) < 32 for ch in text):
        _fail(field_path, "invalid_value", "must not contain control characters")
    return text


def validate_name(value: Any, field_path: str = "name") -> str:
    return _text(value, field_path, MAX_NAME_LENGTH, required=True)


def validate_description(value: Any, field_path: str = "description") -> Optional[str]:
    return _text(value, field_path, MAX_DESCRIPTION_LENGTH, required=False)


def _bool(value: Any, field_path: str) -> bool:
    if not isinstance(value, bool):
        _fail(field_path, "invalid_value", "must be true or false")
    return value


def _int_id(value: Any, field_path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        _fail(field_path, "invalid_value", "must be a positive id")
    return value


def parse_ports(value: Any, field_path: str) -> Optional[str]:
    """Normalise ``22``, ``80,443`` or ``1000:2000`` lists; ``None`` or empty means any port."""
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str):
        _fail(field_path, "ports_invalid", "must be ports such as 22, 80,443 or 1000:2000")
    items = [item.strip() for item in value.split(",")]
    if items == [""]:
        return None
    if len(items) > MAX_PORT_ITEMS:
        _fail(field_path, "ports_too_many", f"must list at most {MAX_PORT_ITEMS} ports or ranges")
    slots = 0
    normalised = []
    for item in items:
        parts = item.split(":")
        if len(parts) > 2 or not all(part.isdigit() for part in parts):
            _fail(field_path, "ports_invalid", f"'{item}' is not a port or a range such as 1000:2000")
        numbers = [int(part) for part in parts]
        if any(number < 1 or number > 65535 for number in numbers):
            _fail(field_path, "ports_invalid", "ports must be from 1 to 65535")
        if len(numbers) == 2 and numbers[0] >= numbers[1]:
            _fail(field_path, "ports_invalid", f"range '{item}' must go from a lower to a higher port")
        slots += len(numbers)
        normalised.append(":".join(str(number) for number in numbers))
    if slots > MAX_PORT_SLOTS:
        _fail(field_path, "ports_too_many", "too many ports: each range counts as two, at most 15 in total")
    if len(set(normalised)) != len(normalised):
        _fail(field_path, "ports_invalid", "must not list a port twice")
    return ",".join(normalised)


def parse_cidr(value: Any, field_path: str, *, allow_any: bool = True) -> tuple[str, int]:
    """Return (canonical text, IP version). A single address is written without a prefix length."""
    if not isinstance(value, str) or not value.strip():
        _fail(field_path, "cidr_invalid", "must be an IP address or network such as 203.0.113.0/24")
    text = value.strip()
    try:
        network = ipaddress.ip_network(text, strict=True)
    except ValueError as exc:
        if "has host bits set" in str(exc):
            _fail(field_path, "cidr_host_bits", f"'{text}' has host bits set; use the network address")
        _fail(field_path, "cidr_invalid", f"'{text}' is not an IP address or network")
    if network.prefixlen == 0 and not allow_any:
        _fail(field_path, "any_network_not_allowed", "a /0 network is not allowed here; leave it empty for any")
    if network.prefixlen == network.max_prefixlen:
        return str(network.network_address), network.version
    return str(network), network.version


def _cidr_list(value: Any, field_path: str) -> tuple[Optional[list[str]], set[int]]:
    if value is None:
        return None, set()
    if not isinstance(value, (list, tuple)):
        _fail(field_path, "cidr_invalid", "must be a list of IP addresses or networks")
    if not value:
        return None, set()
    if len(value) > MAX_CIDRS_PER_RULE:
        _fail(field_path, "cidr_too_many", f"must list at most {MAX_CIDRS_PER_RULE} addresses; use an IP set")
    out: list[str] = []
    versions: set[int] = set()
    for index, item in enumerate(value):
        text, version = parse_cidr(item, f"{field_path}[{index}]")
        if text in out:
            _fail(f"{field_path}[{index}]", "duplicate_entry", f"'{text}' is listed twice")
        out.append(text)
        versions.add(version)
    if len(versions) > 1:
        _fail(field_path, "family_mismatch", "must not mix IPv4 and IPv6 addresses")
    return out, versions


def validate_rule(
    data: Mapping[str, Any],
    field_path: str = "rule",
    *,
    allow_group: bool = True,
    allow_logging: bool = True,
) -> RuleSpec:
    """Validate one rule entry (``kind`` ``rule``, or ``group`` referencing ``group_id``)."""
    if not isinstance(data, Mapping):
        _fail(field_path, "invalid_value", "must be an object")
    unknown = sorted(set(data) - RULE_FIELDS - IGNORED_RULE_FIELDS)
    if unknown:
        _fail(f"{field_path}.{unknown[0]}", "unknown_field", "is not a rule field")

    spec = RuleSpec()
    spec.kind = _choice(data.get("kind", "rule"), FirewallRuleKind, f"{field_path}.kind")
    spec.enabled = _bool(data.get("enabled", True), f"{field_path}.enabled")
    spec.comment = _text(data.get("comment"), f"{field_path}.comment", MAX_COMMENT_LENGTH, required=False)

    if spec.kind == FirewallRuleKind.GROUP.value:
        if not allow_group:
            _fail(f"{field_path}.kind", "group_nesting", "a security group cannot contain another group")
        spec.ref_group_id = _int_id(data.get("group_id"), f"{field_path}.group_id")
        for key in sorted(RULE_FIELDS - {"kind", "group_id", "enabled", "comment"}):
            if data.get(key) not in (None, [], "", False) and not (key == "ip_version" and data[key] == "any"):
                _fail(f"{field_path}.{key}", "invalid_value", "a group reference takes only group_id, enabled and comment")
        return spec

    if data.get("group_id") is not None:
        _fail(f"{field_path}.group_id", "invalid_value", "only a group reference takes group_id")
    if data.get("direction") is None:
        _fail(f"{field_path}.direction", "required", "is required")
    spec.direction = _choice(data.get("direction"), FirewallDirection, f"{field_path}.direction")
    if data.get("action") is None:
        _fail(f"{field_path}.action", "required", "is required")
    spec.action = _choice(data.get("action"), FirewallAction, f"{field_path}.action", upper=True)
    spec.ip_version = _choice(str(data.get("ip_version", "any")), FirewallIpVersion, f"{field_path}.ip_version")

    protocol = data.get("protocol")
    if protocol in (None, "", "any"):
        protocol = None
    elif not isinstance(protocol, str) or protocol.lower() not in PROTOCOLS:
        _fail(f"{field_path}.protocol", "invalid_value", "must be one of: " + ", ".join(PROTOCOLS))
    else:
        protocol = protocol.lower()
    spec.protocol = protocol

    spec.ports = parse_ports(data.get("ports"), f"{field_path}.ports")
    spec.source_ports = parse_ports(data.get("source_ports"), f"{field_path}.source_ports")
    for key in ("ports", "source_ports"):
        if getattr(spec, key) is not None and protocol not in PORT_PROTOCOLS:
            _fail(f"{field_path}.{key}", "ports_without_protocol", "ports need the protocol tcp, udp or sctp")

    icmp_type = data.get("icmp_type")
    if icmp_type in (None, ""):
        spec.icmp_type = None
    else:
        if protocol not in ("icmp", "ipv6-icmp"):
            _fail(f"{field_path}.icmp_type", "icmp_type_without_icmp", "an ICMP type needs the protocol icmp or ipv6-icmp")
        allowed = ICMP_TYPES if protocol == "icmp" else ICMPV6_TYPES
        if not isinstance(icmp_type, str) or icmp_type not in allowed:
            _fail(f"{field_path}.icmp_type", "icmp_type_invalid", f"'{icmp_type}' is not an ICMP type Proxmox knows")
        spec.icmp_type = icmp_type

    spec.remote_cidrs, remote_versions = _cidr_list(data.get("remote_cidrs"), f"{field_path}.remote_cidrs")
    spec.local_cidrs, local_versions = _cidr_list(data.get("local_cidrs"), f"{field_path}.local_cidrs")
    if data.get("remote_ipset_id") is not None:
        if spec.remote_cidrs:
            _fail(f"{field_path}.remote_ipset_id", "remote_conflict", "use either addresses or an IP set, not both")
        spec.remote_ipset_id = _int_id(data.get("remote_ipset_id"), f"{field_path}.remote_ipset_id")

    families = remote_versions | local_versions
    if protocol == "icmp":
        families.add(4)
    elif protocol == "ipv6-icmp":
        families.add(6)
    if spec.ip_version != FirewallIpVersion.ANY.value:
        families.add(int(spec.ip_version))
    if len(families) > 1:
        _fail(f"{field_path}.ip_version", "family_mismatch", "the rule mixes IPv4 and IPv6 (addresses, protocol or IP version)")

    spec.log = _bool(data.get("log", False), f"{field_path}.log")
    if spec.log and not allow_logging:
        _fail(f"{field_path}.log", "logging_not_allowed", "firewall logging is not available")
    return spec


def validate_rules(
    rules: Any,
    field_path: str = "rules",
    *,
    max_count: int,
    allow_group: bool = True,
    allow_logging: bool = True,
    max_group_refs: Optional[int] = None,
) -> list[RuleSpec]:
    """Validate an ordered list; group references are counted and may not repeat."""
    if not isinstance(rules, (list, tuple)):
        _fail(field_path, "invalid_value", "must be a list of rules")
    if len(rules) > max_count:
        _fail(field_path, "limit_exceeded", f"at most {max_count} rules are allowed")
    specs = [
        validate_rule(rule, f"{field_path}[{index}]", allow_group=allow_group, allow_logging=allow_logging)
        for index, rule in enumerate(rules)
    ]
    seen: set[int] = set()
    for index, spec in enumerate(specs):
        if spec.kind != FirewallRuleKind.GROUP.value:
            continue
        if spec.ref_group_id in seen:
            _fail(f"{field_path}[{index}].group_id", "group_already_attached", "this group is already in the list")
        seen.add(spec.ref_group_id)
    if max_group_refs is not None and len(seen) > max_group_refs:
        _fail(field_path, "limit_exceeded", f"at most {max_group_refs} security groups are allowed")
    return specs


def validate_ipset_entries(entries: Any, field_path: str = "entries", *, max_entries: int) -> list[IpsetEntrySpec]:
    """IP set entries: strict CIDRs, no /0, no duplicates, at most ``max_entries``."""
    if not isinstance(entries, (list, tuple)):
        _fail(field_path, "invalid_value", "must be a list of entries")
    if len(entries) > max_entries:
        _fail(field_path, "limit_exceeded", f"at most {max_entries} entries are allowed")
    out: list[IpsetEntrySpec] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        path = f"{field_path}[{index}]"
        if isinstance(entry, str):
            entry = {"cidr": entry}
        if not isinstance(entry, Mapping):
            _fail(path, "invalid_value", "must be an address or an object with cidr")
        unknown = sorted(set(entry) - {"cidr", "nomatch", "comment", "id"})
        if unknown:
            _fail(f"{path}.{unknown[0]}", "unknown_field", "is not an IP set entry field")
        cidr, _version = parse_cidr(entry.get("cidr"), f"{path}.cidr", allow_any=False)
        if cidr in seen:
            _fail(f"{path}.cidr", "duplicate_entry", f"'{cidr}' is listed twice")
        seen.add(cidr)
        out.append(
            IpsetEntrySpec(
                cidr=cidr,
                nomatch=_bool(entry.get("nomatch", False), f"{path}.nomatch"),
                comment=_text(entry.get("comment"), f"{path}.comment", MAX_COMMENT_LENGTH, required=False),
            )
        )
    return out


def validate_baseline_settings(settings: Any, field_path: str = "settings") -> dict[str, Any]:
    """Baseline settings; a missing or null key inherits from the less specific baseline."""
    if settings is None:
        return {}
    if not isinstance(settings, Mapping):
        _fail(field_path, "invalid_value", "must be an object")
    unknown = sorted(set(settings) - BASELINE_SETTING_KEYS)
    if unknown:
        _fail(f"{field_path}.{unknown[0]}", "unknown_field", "is not a baseline setting")
    out: dict[str, Any] = {}
    for key, value in settings.items():
        path = f"{field_path}.{key}"
        if value is None:
            continue
        if key == "smtp_out":
            if value not in SMTP_OUT_VALUES:
                _fail(path, "invalid_value", "must be block, allow or null (inherit)")
            out[key] = value
        elif key == "management_ipset_ids":
            if not isinstance(value, (list, tuple)):
                _fail(path, "invalid_value", "must be a list of IP set ids")
            out[key] = sorted({_int_id(item, f"{path}[{i}]") for i, item in enumerate(value)})
        else:
            out[key] = _bool(value, path)
    return out


def validate_admin_overrides(overrides: Any, field_path: str = "admin_overrides") -> dict[str, Any]:
    """Per-service admin overrides of the merged baseline settings."""
    if overrides is None:
        return {}
    if not isinstance(overrides, Mapping):
        _fail(field_path, "invalid_value", "must be an object")
    unknown = sorted(set(overrides) - ADMIN_OVERRIDE_KEYS)
    if unknown:
        _fail(f"{field_path}.{unknown[0]}", "unknown_field", "is not an admin override")
    out: dict[str, Any] = {}
    for key, value in overrides.items():
        path = f"{field_path}.{key}"
        if value is None:
            continue
        if key == "smtp_out":
            if value not in SMTP_OUT_VALUES:
                _fail(path, "invalid_value", "must be block, allow or null")
            out[key] = value
        elif key == "extra_ipfilter":
            if not isinstance(value, (list, tuple)):
                _fail(path, "invalid_value", "must be a list of addresses")
            cidrs = []
            for index, item in enumerate(value):
                cidr, _version = parse_cidr(item, f"{path}[{index}]", allow_any=False)
                if cidr not in cidrs:
                    cidrs.append(cidr)
            if len(cidrs) > MAX_CIDRS_PER_RULE:
                _fail(path, "cidr_too_many", f"must list at most {MAX_CIDRS_PER_RULE} addresses")
            out[key] = cidrs
        else:
            out[key] = _bool(value, path)
    return out


def validate_prefixes(value: Any, field_path: str = "ipv6_allowed_prefixes") -> Optional[list[str]]:
    """Allowed IPv6 prefixes of a location; ``None`` or empty means none."""
    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        _fail(field_path, "invalid_value", "must be a list of IPv6 prefixes")
    out: list[str] = []
    for index, item in enumerate(value):
        cidr, version = parse_cidr(item, f"{field_path}[{index}]", allow_any=False)
        if version != 6:
            _fail(f"{field_path}[{index}]", "family_mismatch", "must be an IPv6 prefix")
        if cidr not in out:
            out.append(cidr)
    if len(out) > MAX_CIDRS_PER_RULE:
        _fail(field_path, "cidr_too_many", f"must list at most {MAX_CIDRS_PER_RULE} prefixes")
    return out or None


def check_reference(obj: Any, service_owner_id: Optional[int], *, actor_is_staff: bool, field_path: str) -> None:
    """A client may reference only objects owned by the service's owner, or admin objects marked
    ``client_selectable``. Staff may reference any admin object and the service owner's objects,
    never another client's (that would leak one client's addresses into another's VM)."""
    scope = obj.scope.value if hasattr(obj.scope, "value") else obj.scope
    if scope == FirewallObjectScope.ADMIN.value:
        if actor_is_staff or obj.client_selectable:
            return
    elif service_owner_id is not None and obj.owner_user_id == service_owner_id:
        return
    _fail(field_path, "reference_not_allowed", "this group or IP set cannot be used on this server")


def check_count(count: int, limit: int, field_path: str, what: str) -> None:
    if count > limit:
        _fail(field_path, "limit_exceeded", f"at most {limit} {what} are allowed")


def unique_ids(ids: Iterable[Any], field_path: str) -> list[int]:
    out: list[int] = []
    for index, value in enumerate(ids):
        rule_id = _int_id(value, f"{field_path}[{index}]")
        if rule_id in out:
            _fail(f"{field_path}[{index}]", "duplicate_entry", "is listed twice")
        out.append(rule_id)
    return out


def specs_from_rows(rows: Sequence[Any]) -> list[RuleSpec]:
    """``RuleSpec`` of stored ``FirewallRule`` rows (for comparisons and audit metadata)."""
    out = []
    for row in rows:
        out.append(
            RuleSpec(
                kind=_value(row.kind),
                ref_group_id=row.ref_group_id,
                direction=_value(row.direction),
                action=_value(row.action),
                ip_version=_value(row.ip_version) or FirewallIpVersion.ANY.value,
                protocol=row.protocol,
                ports=row.ports,
                source_ports=row.source_ports,
                icmp_type=row.icmp_type,
                remote_cidrs=list(row.remote_cidrs) if row.remote_cidrs else None,
                remote_ipset_id=row.remote_ipset_id,
                local_cidrs=list(row.local_cidrs) if row.local_cidrs else None,
                enabled=bool(row.enabled),
                log=bool(row.log),
                comment=row.comment,
            )
        )
    return out


def _value(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value
