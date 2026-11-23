// Wording shared by every VM firewall screen. The interruption figure is measured, so it lives here only.

export const INTERRUPTION_WARNING =
  "Turning the firewall on briefly interrupts this server's network (under half a second).";

/** The same warning for the bulk rollout, which reaches many servers. */
export const ROLLOUT_INTERRUPTION_WARNING =
  'Each running server that does not yet have the firewall switched on for its network card will briefly interrupt its network (under half a second) when the baseline reaches it.';

export const STATE_INFO = {
  off: { label: 'Off', variant: 'muted', text: 'Nothing is being filtered on this server.' },
  pending: { label: 'Applying', variant: 'info', text: 'A change is waiting to be written to the hypervisor.' },
  applied: { label: 'Active', variant: 'success', text: 'The firewall is in place and matches your settings.' },
  pending_reboot: {
    label: 'Active after restart',
    variant: 'warning',
    text: "The rules are written, but the network card's firewall switch only changes when the server is restarted.",
  },
  error: { label: 'Error', variant: 'danger', text: 'The last attempt to apply the firewall failed. It is retried automatically.' },
  drift: { label: 'Out of sync', variant: 'warning', text: 'The firewall on the hypervisor differs from what is configured here.' },
  not_enforced: {
    label: 'Not enforced',
    variant: 'warning',
    text: 'The rules are written, but the hypervisor firewall is not currently switched on, so nothing is filtered.',
  },
  unmanaged: { label: 'Not managed', variant: 'muted', text: 'Staff have set this server so that Rackflow does not touch its firewall.' },
  vm_missing: { label: 'Server missing', variant: 'danger', text: 'The hypervisor has no such server.' },
};

export const REASON_TEXT = {
  location_disabled: "The VM firewall is not switched on for this server's location.",
  excluded: 'This service is marked as infrastructure and is never managed.',
  not_vm: 'This service is not a virtual machine.',
  no_vm_record: 'This service has no placed virtual machine yet.',
  no_net0: 'The server has no first network card (net0), so there is nothing to filter.',
  nic_trunk: 'A network card carries trunked VLANs, which the firewall cannot filter safely.',
  private_bridge_trunk: 'A network card is on the private network bridge without a VLAN tag.',
  infrastructure_bridge: 'A network card is on an infrastructure bridge that the firewall must not touch.',
  storage_mac: 'A network card looks like a storage network card.',
  ipv6_ra_bridge_needs_prefix:
    'net0 is on a bridge where servers choose their own IPv6 addresses from router adverts, and the location has no IPv6 allowed prefixes. Address anti-spoofing is held off for this server until a prefix is added to the location.',
  net0_bridge_unknown:
    'Rackflow has not yet seen which bridge net0 is on, and the location has router-advert bridges. Address anti-spoofing is held off until the next check.',
};

export function reasonText(code) {
  return REASON_TEXT[code] || String(code || '').replaceAll('_', ' ');
}

export const PRESET_FALLBACK = [
  { key: 'ssh', label: 'SSH', description: 'Allow SSH (TCP 22) from anywhere.' },
  { key: 'rdp', label: 'Remote Desktop', description: 'Allow Remote Desktop (TCP 3389) from anywhere.' },
  { key: 'web', label: 'Web server', description: 'Allow HTTP and HTTPS from anywhere.' },
  { key: 'ping', label: 'Ping', description: 'Allow ping over IPv4 and IPv6.' },
  { key: 'allow_all', label: 'Allow everything', description: 'Accept all incoming traffic; your rules can still block specific traffic.' },
];
