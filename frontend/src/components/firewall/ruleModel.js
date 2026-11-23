// Draft rules for the editors: the server shape turned into something a form can bind to, and back.

export const PROTOCOLS = ['tcp', 'udp', 'icmp', 'ipv6-icmp', 'sctp', 'gre', 'esp', 'ah'];
export const PORT_PROTOCOLS = ['tcp', 'udp', 'sctp'];
export const ICMP_TYPES = [
  'echo-request',
  'echo-reply',
  'destination-unreachable',
  'time-exceeded',
  'parameter-problem',
  'redirect',
  'router-advertisement',
  'router-solicitation',
  'neighbour-solicitation',
  'neighbour-advertisement',
  'packet-too-big',
];
export const POLICIES = ['DROP', 'REJECT', 'ACCEPT'];
export const ACTIONS = ['ACCEPT', 'DROP', 'REJECT'];

export const ACTION_LABEL = { ACCEPT: 'Allow', DROP: 'Drop', REJECT: 'Reject' };
export const POLICY_LABEL = {
  DROP: 'Drop (block silently)',
  REJECT: 'Reject (block and tell the sender)',
  ACCEPT: 'Allow',
};

let counter = 0;
const nextKey = () => `r${++counter}`;

const splitList = (text) => String(text || '').split(/[\s,]+/).filter(Boolean);

export function blankRule() {
  return {
    _key: nextKey(),
    id: null,
    kind: 'rule',
    group_id: null,
    enabled: true,
    comment: '',
    direction: 'in',
    action: 'ACCEPT',
    ip_version: 'any',
    protocol: '',
    ports: '',
    source_ports: '',
    icmp_type: '',
    remoteMode: 'any',
    cidrsText: '',
    ipsetId: '',
    localText: '',
    log: false,
  };
}

export function blankGroupRef(groupId) {
  return { ...blankRule(), kind: 'group', group_id: groupId };
}

/** Server rule to draft. */
export function toDraft(rule) {
  const draft = blankRule();
  draft.id = rule.id ?? null;
  draft.kind = rule.kind || 'rule';
  draft.group_id = rule.group_id ?? rule.ref_group_id ?? null;
  draft.enabled = rule.enabled !== false;
  draft.comment = rule.comment || '';
  if (draft.kind === 'group') return draft;
  draft.direction = rule.direction || 'in';
  draft.action = rule.action || 'ACCEPT';
  draft.ip_version = String(rule.ip_version || 'any');
  draft.protocol = rule.protocol || '';
  draft.ports = rule.ports || '';
  draft.source_ports = rule.source_ports || '';
  draft.icmp_type = rule.icmp_type || '';
  if (rule.remote_ipset_id) {
    draft.remoteMode = 'ipset';
    draft.ipsetId = String(rule.remote_ipset_id);
  } else if (rule.remote_cidrs && rule.remote_cidrs.length) {
    draft.remoteMode = 'cidrs';
    draft.cidrsText = rule.remote_cidrs.join(', ');
  }
  draft.localText = (rule.local_cidrs || []).join(', ');
  draft.log = !!rule.log;
  return draft;
}

/** Draft to the body the server accepts (it refuses unknown fields and fields that do not fit the protocol). */
export function toPayload(draft) {
  const base = { kind: draft.kind, enabled: !!draft.enabled, comment: draft.comment.trim() || null };
  if (draft.id != null) base.id = draft.id;
  if (draft.kind === 'group') return { ...base, group_id: draft.group_id };
  const protocol = draft.protocol || null;
  const hasPorts = PORT_PROTOCOLS.includes(protocol);
  const isIcmp = protocol === 'icmp' || protocol === 'ipv6-icmp';
  const cidrs = draft.remoteMode === 'cidrs' ? splitList(draft.cidrsText) : [];
  const local = splitList(draft.localText);
  return {
    ...base,
    direction: draft.direction,
    action: draft.action,
    ip_version: draft.ip_version,
    protocol,
    ports: hasPorts ? draft.ports.trim() || null : null,
    source_ports: hasPorts ? draft.source_ports.trim() || null : null,
    icmp_type: isIcmp ? draft.icmp_type.trim() || null : null,
    remote_cidrs: cidrs.length ? cidrs : null,
    remote_ipset_id: draft.remoteMode === 'ipset' && draft.ipsetId ? Number(draft.ipsetId) : null,
    local_cidrs: local.length ? local : null,
    log: !!draft.log,
  };
}

export const payloadOf = (drafts) => drafts.map(toPayload);

/** Whether two draft lists would save the same thing. */
export const sameDrafts = (a, b) => JSON.stringify(payloadOf(a)) === JSON.stringify(payloadOf(b));

const PROTOCOL_LABEL = { tcp: 'TCP', udp: 'UDP', icmp: 'ICMP', 'ipv6-icmp': 'ICMPv6', sctp: 'SCTP', gre: 'GRE', esp: 'ESP', ah: 'AH' };
export const protocolLabel = (protocol) => (protocol ? PROTOCOL_LABEL[protocol] || protocol : 'Any');

/** The far end of a draft rule in words. */
export function remoteLabel(draft, ipsets = []) {
  if (draft.remoteMode === 'ipset') {
    const set = ipsets.find((item) => String(item.id) === String(draft.ipsetId));
    return set ? `IP set ${set.name}` : `IP set #${draft.ipsetId}`;
  }
  if (draft.remoteMode === 'cidrs') return splitList(draft.cidrsText).join(', ') || 'Anywhere';
  return 'Anywhere';
}

/** Server rules that are read only (the baseline): same labelling through a draft. */
export function describeServerRule(rule, ipsets = []) {
  const draft = toDraft(rule);
  return {
    draft,
    action: ACTION_LABEL[draft.action] || draft.action,
    direction: draft.direction === 'in' ? 'Incoming' : 'Outgoing',
    protocol: protocolLabel(draft.protocol),
    ports: draft.ports || (draft.icmp_type ? draft.icmp_type : 'Any'),
    remote: remoteLabel(draft, ipsets),
  };
}

/** Field errors keyed by their path (`rules[2].ports`) from a thrown FirewallApiError. */
export function fieldErrorMap(err) {
  return err && err.field ? { [err.field]: err.message } : {};
}
