<script>
  import { onMount } from 'svelte';
  import { Alert, Button, Spinner } from '../ui/index.js';
  import {
    clientListPrivateNetworks,
    clientPrivateNetworkOptions,
    clientCreatePrivateNetwork,
    clientDeletePrivateNetwork,
    clientAttachPrivateNetwork,
    clientDetachPrivateNetwork,
    clientNetworkInvitations,
    clientAcceptNetworkInvitation,
    clientDeclineNetworkInvitation,
    clientInviteToNetwork,
    clientCancelNetworkInvitation,
    clientRemoveNetworkMember,
    clientSharedNetworks,
    clientSharedNetwork,
    clientSharedNetworkStorage,
    clientSharedAttach,
    clientSharedDetach,
    clientLeaveSharedNetwork,
    clientFastPath,
    clientFastPathOn,
    clientFastPathOff,
  } from '../../lib/api.js';
  import PrivateNetworkDhcp from '../PrivateNetworkDhcp.svelte';
  import { copyText } from '../../lib/copyText.js';

  const STATE_LABELS = { pending: 'Connecting', active: 'Connected', error: 'Being retried', detaching: 'Disconnecting' };
  const emptyCreateForm = () => ({ location_id: '', name: '', cidr: '' });

  let networks = [];
  let invitations = [];
  let shared = [];
  // Shared network id -> its storage address and open shares
  let sharedStorage = {};
  // Shared network id -> the effective DHCP settings the owner has set
  let sharedDhcp = {};
  // Owned network id -> address or username being invited
  let inviteForms = {};
  let fastPath = { cidr: '', locations: [], endpoints: [] };
  let options = { default_cidr: '', locations: [], services: [] };
  let loading = true;
  let working = false;
  let error = '';
  let success = '';
  let showCreate = false;
  let createForm = emptyCreateForm();
  // Per network: { service_id, ip_address }
  let attachForms = {};

  $: creatableLocations = options.locations.filter((l) => l.networks_in_use < l.max_networks);
  $: canCreate = creatableLocations.length > 0;

  const TYPE_LABELS = { vm: 'Virtual machine', bare_metal: 'Dedicated server', nfs_storage: 'NFS storage' };
  const typeLabel = (type) => TYPE_LABELS[type] || 'Service';

  function attachForm(networkId) {
    return attachForms[networkId] || { service_id: '', ip_address: '' };
  }

  function setAttachField(networkId, field, value) {
    attachForms = { ...attachForms, [networkId]: { ...attachForm(networkId), [field]: value } };
  }

  function attachableServices(network) {
    return options.services.filter(
      (s) => s.location_id === network.location_id && !network.attachments.some((a) => a.service_id === s.service_id),
    );
  }

  async function load() {
    try {
      [networks, options, invitations, shared] = await Promise.all([
        clientListPrivateNetworks(),
        clientPrivateNetworkOptions(),
        clientNetworkInvitations(),
        clientSharedNetworks(),
      ]);
      const details = await Promise.all(
        shared.map((n) =>
          Promise.all([
            clientSharedNetworkStorage(n.id).catch(() => ({ linked: false, shares: [] })),
            clientSharedNetwork(n.id).catch(() => null),
          ]),
        ),
      );
      sharedStorage = Object.fromEntries(shared.map((n, i) => [n.id, details[i][0]]));
      sharedDhcp = Object.fromEntries(shared.map((n, i) => [n.id, details[i][1]?.dhcp || null]));
      try {
        fastPath = await clientFastPath();
      } catch {
        fastPath = { cidr: '', locations: [], endpoints: [] };
      }
    } catch (err) {
      error = err.message || 'Failed to load private networks';
    } finally {
      loading = false;
    }
  }

  async function run(action, message) {
    working = true;
    error = '';
    success = '';
    try {
      await action();
      success = message;
      await load();
      return true;
    } catch (err) {
      error = err.message || 'Request failed';
      return false;
    } finally {
      working = false;
    }
  }

  async function submitCreate() {
    const created = await run(
      () =>
        clientCreatePrivateNetwork({
          location_id: Number(createForm.location_id),
          name: createForm.name.trim(),
          cidr: createForm.cidr.trim() || null,
        }),
      'Private network created.',
    );
    if (created) {
      showCreate = false;
      createForm = emptyCreateForm();
    }
  }

  async function submitAttach(network) {
    const form = attachForm(network.id);
    const attached = await run(
      () =>
        clientAttachPrivateNetwork(network.id, {
          service_id: Number(form.service_id),
          ip_address: form.ip_address.trim() || null,
        }),
      'Service connected.',
    );
    if (attached) attachForms = { ...attachForms, [network.id]: { service_id: '', ip_address: '' } };
  }

  function detach(network, attachment) {
    if (!confirm(`Disconnect ${attachment.service_name || 'this service'} from ${network.name}?`)) return null;
    return run(() => clientDetachPrivateNetwork(network.id, attachment.id), 'Service disconnected.');
  }

  async function submitInvite(network) {
    const who = (inviteForms[network.id] || '').trim();
    const sent = await run(
      () => clientInviteToNetwork(network.id, who),
      'Invitation sent. If the address or username belongs to a client who can join, they will see it in their portal.',
    );
    if (sent) inviteForms = { ...inviteForms, [network.id]: '' };
  }

  function cancelInvite(network, invitation) {
    return run(() => clientCancelNetworkInvitation(network.id, invitation.id), 'Invitation cancelled.');
  }

  function removeMember(network, member) {
    const warning = `Remove ${member.username || 'this member'} from ${network.name}? Their servers are taken off the network at once.`;
    if (!confirm(warning)) return null;
    return run(() => clientRemoveNetworkMember(network.id, member.user_id), 'Member removed.');
  }

  function answer(invitation, accept) {
    return run(
      () => (accept ? clientAcceptNetworkInvitation(invitation.id) : clientDeclineNetworkInvitation(invitation.id)),
      accept ? 'You joined the network.' : 'Invitation declined.',
    );
  }

  async function submitSharedAttach(network) {
    const key = `s${network.id}`;
    const form = attachForm(key);
    const attached = await run(
      () =>
        clientSharedAttach(network.id, {
          service_id: Number(form.service_id),
          ip_address: form.ip_address.trim() || null,
        }),
      'Service connected.',
    );
    if (attached) attachForms = { ...attachForms, [key]: { service_id: '', ip_address: '' } };
  }

  function sharedDetach(network, attachment) {
    if (!confirm(`Disconnect ${attachment.service_name || 'this service'} from ${network.name}?`)) return null;
    return run(() => clientSharedDetach(network.id, attachment.id), 'Service disconnected.');
  }

  function leave(network) {
    const warning = `Leave ${network.name}? Your servers on it are taken off the network and you lose access to its storage.`;
    if (!confirm(warning)) return null;
    return run(() => clientLeaveSharedNetwork(network.id), 'You left the network.');
  }

  function removeNetwork(network) {
    if (!confirm(`Delete private network ${network.name}?`)) return null;
    return run(() => clientDeletePrivateNetwork(network.id), 'Private network deleted.');
  }

  function fastPathOn(loc) {
    return run(
      () => clientFastPathOn(loc.location_id),
      `Proxy fast path on in ${loc.location_name}. Connect your servers to the "Proxy fast path" network below.`,
    );
  }

  function fastPathOff(loc) {
    if (!confirm(`Turn the proxy fast path off in ${loc.location_name}?`)) return null;
    return run(() => clientFastPathOff(loc.location_id), `Proxy fast path off in ${loc.location_name}.`);
  }

  async function copy(text) {
    error = '';
    try {
      await copyText(text);
      success = 'Copied.';
    } catch {
      error = 'Clipboard unavailable';
    }
  }

  // ip:port:user:pass when the client may see the credentials, else just ip:port.
  const endpointLine = (ep) => ep.endpoint || `${ep.fast_path_ip}:${ep.port}`;

  function locationLines(loc) {
    const lines = fastPath.endpoints
      .filter((ep) => ep.location_id === loc.location_id && ep.active)
      .map(endpointLine);
    return [...new Set(lines)];
  }

  $: withCredentials = fastPath.endpoints.some((ep) => ep.endpoint);

  const list = (items) => (items || []).join(', ');
  const leaseText = (seconds) => {
    if (seconds % 86400 === 0) return `${seconds / 86400} day${seconds === 86400 ? '' : 's'}`;
    if (seconds % 3600 === 0) return `${seconds / 3600} hour${seconds === 3600 ? '' : 's'}`;
    return `${seconds} seconds`;
  };

  onMount(load);
</script>

<section aria-labelledby="networks-title">
  <div class="page-head">
    <div>
      <h1 id="networks-title">Private networks</h1>
      <p class="sub">Connect your virtual machines and dedicated servers in one location over an isolated, unmetered network.</p>
    </div>
    {#if canCreate && !showCreate}
      <Button on:click={() => (showCreate = true)}>New network</Button>
    {/if}
  </div>

  {#if error}<Alert type="danger">{error}</Alert>{/if}
  {#if success}<Alert type="success">{success}</Alert>{/if}

  {#if loading}
    <div class="state"><Spinner /><span>Loading…</span></div>
  {:else}
    {#if showCreate}
      <div class="panel">
        <h2>New private network</h2>
        <label>Location
          <select bind:value={createForm.location_id}>
            <option value="">Select a location</option>
            {#each creatableLocations as loc (loc.location_id)}
              <option value={loc.location_id}>
                {loc.location_name} ({loc.networks_in_use} of {loc.max_networks} used)
              </option>
            {/each}
          </select>
        </label>
        <label>Name <input bind:value={createForm.name} maxlength="64" placeholder="backend" /></label>
        <label>Address range
          <input bind:value={createForm.cidr} placeholder={options.default_cidr} />
          <small>Optional. A private IPv4 range such as {options.default_cidr}, between /16 and /29. The last usable address is kept for storage.</small>
        </label>
        <div class="actions">
          <Button variant="secondary" on:click={() => (showCreate = false)}>Cancel</Button>
          <Button on:click={submitCreate} disabled={working || !createForm.location_id || !createForm.name.trim()}>
            Create
          </Button>
        </div>
      </div>
    {/if}

    {#if fastPath.locations.length > 0}
      <div class="panel">
        <h2>Proxy fast path</h2>
        <p class="hint">
          Reach your proxies from your servers over a private network instead of the internet. Your servers get an
          address in <span class="mono">{fastPath.cidr}</span> and connect to the private address of each proxy with
          the same username and password; traffic still leaves from the proxy's public IP.
        </p>
        <div class="rows">
          {#each fastPath.locations as loc (loc.location_id)}
            <div class="row">
              <div><span>{loc.location_name}</span></div>
              <div>
                {#if loc.enabled}
                  <span class="badge ok">On</span>
                  <small>{loc.servers_attached} server{loc.servers_attached === 1 ? '' : 's'} connected</small>
                {:else if loc.status === 'deleting'}
                  <span class="badge warn">Turning off</span>
                {:else}
                  <span class="badge">Off</span>
                {/if}
              </div>
              <div>
                {#if !loc.available}
                  <small>No longer offered here</small>
                {:else if !loc.eligible && !loc.enabled && loc.reason}
                  <small>{loc.reason}.</small>
                {/if}
              </div>
              <div class="row-end">
                {#if loc.enabled && locationLines(loc).length > 0}
                  <button class="link plain copy-all" on:click={() => copy(locationLines(loc).join('\n'))}>
                    Copy all ({withCredentials ? 'ip:port:user:pass' : 'ip:port'})
                  </button>
                {/if}
                {#if loc.enabled}
                  <button
                    class="link"
                    disabled={working || loc.servers_attached > 0}
                    title={loc.servers_attached > 0 ? 'Disconnect your servers from the network first' : ''}
                    on:click={() => fastPathOff(loc)}
                  >Turn off</button>
                {:else if loc.available && !loc.status && loc.eligible}
                  <Button size="small" disabled={working} on:click={() => fastPathOn(loc)}>Turn on</Button>
                {/if}
              </div>
            </div>
          {/each}
        </div>
        {#if fastPath.endpoints.length > 0}
          <h3>Private proxy addresses</h3>
          <div class="rows">
            {#each fastPath.endpoints as ep (ep.assignment_id)}
              <div class="row">
                <div>
                  <a href={`/client/services/${ep.service_id}`}>{ep.service_name}</a>
                  <small class="mono">{ep.public_ip}</small>
                </div>
                <div>
                  <span class="mono">{ep.fast_path_ip}:{ep.port}</span>
                  <small>{ep.location_name}</small>
                </div>
                <div>
                  <span class="badge" class:ok={ep.active}>{ep.active ? 'Ready' : 'Turn on in this location'}</span>
                </div>
                <div class="row-end">
                  <button class="link plain" on:click={() => copy(endpointLine(ep))}>Copy</button>
                </div>
              </div>
            {/each}
          </div>
          <p class="hint">
            Proxy IPs in the same subnet share one private address; your username and password pick which public IP
            is used.{#if withCredentials} Copy gives <span class="mono">ip:port:user:pass</span>, the public line with the
            private address as host.{/if}
          </p>
        {/if}
      </div>
    {/if}

    {#if invitations.length > 0}
      <div class="panel">
        <h2>Invitations</h2>
        <p class="hint">Other clients have invited you to join their private networks. Joining lets you connect your own servers and reach the storage they share on it.</p>
        <div class="rows">
          {#each invitations as invitation (invitation.id)}
            <div class="row">
              <div>
                <span>{invitation.network_name}</span>
                <small>from {invitation.owner}</small>
              </div>
              <div>{invitation.location_name}</div>
              <div><small>Expires {invitation.expires_at ? new Date(invitation.expires_at).toLocaleDateString() : ''}</small></div>
              <div class="row-end">
                <Button size="small" disabled={working} on:click={() => answer(invitation, true)}>Accept</Button>
                <button class="link" disabled={working} on:click={() => answer(invitation, false)}>Decline</button>
              </div>
            </div>
          {/each}
        </div>
      </div>
    {/if}

    {#if networks.length === 0 && shared.length === 0 && !showCreate}
      <div class="empty">
        <h2>No private networks yet</h2>
        {#if options.locations.length === 0}
          <p>Private networks are available once you have an active virtual machine, dedicated server or NFS storage in a supported location.</p>
        {:else if !canCreate}
          <p>You have reached the limit of private networks for your locations.</p>
        {:else}
          <p>Create a network, then connect the services that should reach each other privately.</p>
        {/if}
      </div>
    {/if}

    {#each networks as network (network.id)}
      {@const candidates = attachableServices(network)}
      {@const form = attachForms[network.id] || { service_id: '', ip_address: '' }}
      <article class="panel">
        <div class="head">
          <div>
            <h2>{network.name}</h2>
            <p>
              {network.location_name} · <span class="mono">{network.cidr}</span> · VLAN
              <span class="mono">{network.vlan_id}</span>
              {#if network.kind === 'proxy_access'}
                · private access to your proxies; <span class="mono">{fastPath.reserved_block || ''}</span> is kept for proxy addresses
              {:else if network.storage_endpoint_ip}
                · <span class="mono">{network.storage_endpoint_ip}</span> reserved for storage
              {/if}
            </p>
          </div>
          {#if network.status === 'deleting'}
            <span class="badge warn">Deleting</span>
          {:else if network.kind === 'proxy_access'}
            <span class="badge ok">Proxy fast path</span>
          {:else}
            <Button
              variant="danger"
              size="small"
              disabled={working || network.attachments.length > 0}
              title={network.attachments.length > 0 ? 'Disconnect every service first' : 'Delete this network'}
              on:click={() => removeNetwork(network)}
            >Delete</Button>
          {/if}
        </div>

        {#if network.attachments.length === 0}
          <p class="none">No services connected.</p>
        {:else}
          <div class="rows">
            {#each network.attachments as attachment (attachment.id)}
              <div class="row">
                <div>
                  {#if attachment.service_id}
                    <a href={`/client/services/${attachment.service_id}`}>{attachment.service_name}</a>
                  {:else}
                    <span>Removed service</span>
                  {/if}
                  <small>{typeLabel(attachment.service_type)}</small>
                </div>
                <div>
                  <span class="mono">{attachment.ip_address}/{attachment.prefix_length}</span>
                  {#if attachment.mac_address}<small class="mono">{attachment.mac_address}</small>{/if}
                </div>
                <div>
                  <span class="badge" class:ok={attachment.state === 'active'} class:warn={attachment.state !== 'active'}>
                    {STATE_LABELS[attachment.state] || attachment.state}
                  </span>
                </div>
                <div class="row-end">
                  {#if attachment.service_type === 'nfs_storage'}
                    <a href="/client/storage">Storage</a>
                  {:else if attachment.state !== 'detaching'}
                    <button class="link" disabled={working} on:click={() => detach(network, attachment)}>Disconnect</button>
                  {/if}
                </div>
              </div>
            {/each}
          </div>
          {#if network.kind === 'proxy_access'}
            <p class="hint">
              Set each address inside the operating system. On a virtual machine, use the extra network card with the MAC
              address shown. On a dedicated server, add a VLAN interface with ID {network.vlan_id} on the main network card
              and give it the address shown.
            </p>
          {:else}
            <div class="hint">
              Where the location has storage, your machines get these addresses by DHCP:
              <ul>
                <li>
                  <strong>Virtual machine:</strong> the new network card (the MAC address shown) gets its address by DHCP.
                  Windows and most desktop systems pick it up at once. On a Linux server, turn on DHCP for the new interface,
                  for example with <code>dhclient</code>, a netplan entry or NetworkManager. Templates with cloud-init also
                  get the address written on their next reboot.
                </li>
                <li>
                  <strong>Dedicated server:</strong> add a VLAN interface with ID {network.vlan_id} on the main network card
                  (or the bond) and set it to DHCP. It asks with that card's MAC address, which we know, so it gets the
                  address shown. An interface with its own MAC address needs a reservation (see DHCP below) or the address
                  shown, set by hand.
                </li>
                <li>
                  <strong>When DHCP does not apply:</strong> the location has no storage node, DHCP is switched off for this
                  network, or every storage node of the location is down. Then set the address shown inside the operating
                  system by hand.
                </li>
              </ul>
            </div>
          {/if}
        {/if}

        {#if network.status === 'active'}
          {#if candidates.length > 0}
            <div class="attach">
              <select
                value={form.service_id}
                on:change={(e) => setAttachField(network.id, 'service_id', e.target.value)}
                aria-label="Service to connect"
              >
                <option value="">Connect a service</option>
                {#each candidates as service (service.service_id)}
                  <option value={service.service_id}>{service.name} ({typeLabel(service.service_type)})</option>
                {/each}
              </select>
              <input
                value={form.ip_address}
                on:input={(e) => setAttachField(network.id, 'ip_address', e.target.value)}
                placeholder="Address (optional)"
                aria-label="Private address"
              />
              <Button size="small" disabled={working || !form.service_id} on:click={() => submitAttach(network)}>
                Connect
              </Button>
            </div>
          {:else if network.attachments.length === 0}
            <p class="hint">You have no other active services in {network.location_name} to connect.</p>
          {/if}
        {/if}

        {#if network.kind !== 'proxy_access' && network.status === 'active'}
          <PrivateNetworkDhcp networkId={network.id} />

          <div class="sharing">
            <h3>Members and invitations</h3>
            <p class="hint">
              Invite another client by email address or username. Members can connect their own servers in {network.location_name}
              to this network, and reach any storage share open to the whole network. They cannot change the network, its storage
              or see your other services.
            </p>
            {#if network.members?.length}
              <div class="rows">
                {#each network.members as member (member.user_id)}
                  <div class="row">
                    <div><span>{member.username}</span></div>
                    <div>
                      {#if member.servers.length}
                        {#each member.servers as server}<span class="mono">{server.ip_address}</span> <small>{typeLabel(server.kind)}</small>{/each}
                      {:else}
                        <small>No servers connected</small>
                      {/if}
                    </div>
                    <div><small>Joined {member.joined_at ? new Date(member.joined_at).toLocaleDateString() : ''}</small></div>
                    <div class="row-end">
                      <button class="link" disabled={working} on:click={() => removeMember(network, member)}>Remove</button>
                    </div>
                  </div>
                {/each}
              </div>
            {/if}
            {#if network.invitations?.length}
              <div class="rows">
                {#each network.invitations as invitation (invitation.id)}
                  <div class="row">
                    <div><span>{invitation.invitee}</span></div>
                    <div><span class="badge warn">Invited</span></div>
                    <div><small>Expires {invitation.expires_at ? new Date(invitation.expires_at).toLocaleDateString() : ''}</small></div>
                    <div class="row-end">
                      <button class="link" disabled={working} on:click={() => cancelInvite(network, invitation)}>Cancel</button>
                    </div>
                  </div>
                {/each}
              </div>
            {/if}
            {#if network.member_limit > 0}
              <div class="invite">
                <input
                  value={inviteForms[network.id] || ''}
                  on:input={(e) => (inviteForms = { ...inviteForms, [network.id]: e.target.value })}
                  placeholder="Email address or username"
                  aria-label="Client to invite"
                />
                <Button size="small" disabled={working || !(inviteForms[network.id] || '').trim()} on:click={() => submitInvite(network)}>
                  Invite
                </Button>
              </div>
              <p class="hint">
                {(network.members || []).length} member{(network.members || []).length === 1 ? '' : 's'} and
                {(network.invitations || []).length} pending, up to {network.member_limit} in all. You are always told an invitation was sent,
                whether or not the address matches an account.
              </p>
            {:else}
              <p class="hint">Sharing is not available in this location.</p>
            {/if}
          </div>
        {/if}
      </article>
    {/each}

    {#if shared.length > 0}
      <h2 class="section">Shared with me</h2>
    {/if}
    {#each shared as network (network.id)}
      {@const candidates = attachableServices(network)}
      {@const form = attachForm(`s${network.id}`)}
      {@const storage = sharedStorage[network.id] || { linked: false, shares: [] }}
      <article class="panel">
        <div class="head">
          <div>
            <h2>{network.name}</h2>
            <p>
              Shared by {network.owner || 'another client'} · {network.location_name} · <span class="mono">{network.cidr}</span>
            </p>
          </div>
          <Button variant="danger" size="small" disabled={working} on:click={() => leave(network)}>Leave</Button>
        </div>

        {#if network.attachments.length === 0}
          <p class="none">None of your services are connected.</p>
        {:else}
          <div class="rows">
            {#each network.attachments as attachment (attachment.id)}
              <div class="row">
                <div>
                  <a href={`/client/services/${attachment.service_id}`}>{attachment.service_name}</a>
                  <small>{typeLabel(attachment.service_type)}</small>
                </div>
                <div>
                  <span class="mono">{attachment.ip_address}/{attachment.prefix_length}</span>
                  {#if attachment.mac_address}<small class="mono">{attachment.mac_address}</small>{/if}
                </div>
                <div>
                  <span class="badge" class:ok={attachment.state === 'active'} class:warn={attachment.state !== 'active'}>
                    {STATE_LABELS[attachment.state] || attachment.state}
                  </span>
                </div>
                <div class="row-end">
                  {#if attachment.state !== 'detaching'}
                    <button class="link" disabled={working} on:click={() => sharedDetach(network, attachment)}>Disconnect</button>
                  {/if}
                </div>
              </div>
            {/each}
          </div>
          <div class="hint">
            Where the location has storage, your machines get these addresses by DHCP:
            <ul>
              <li>
                <strong>Virtual machine:</strong> the new network card (the MAC address shown) gets its address by DHCP.
                Windows and most desktop systems pick it up at once. On a Linux server, turn on DHCP for the new interface,
                for example with <code>dhclient</code>, a netplan entry or NetworkManager. Templates with cloud-init also get
                the address written on their next reboot.
              </li>
              {#if network.server_vlan_id}
                <li>
                  <strong>Dedicated server:</strong> add a VLAN interface with ID {network.server_vlan_id} on the main network
                  card (or the bond) and set it to DHCP. It asks with that card's MAC address, which we know, so it gets the
                  address shown. An interface with its own MAC address needs the network owner to reserve an address for it,
                  or the address shown, set by hand.
                </li>
              {/if}
              <li>
                <strong>When DHCP does not apply:</strong> the location has no storage node, the owner has switched DHCP off
                for this network, or every storage node of the location is down. Then set the address shown inside the
                operating system by hand.
              </li>
            </ul>
          </div>
        {/if}

        {#if network.status === 'active'}
          {#if candidates.length > 0}
            <div class="attach">
              <select
                value={form.service_id}
                on:change={(e) => setAttachField(`s${network.id}`, 'service_id', e.target.value)}
                aria-label="Service to connect"
              >
                <option value="">Connect a service</option>
                {#each candidates as service (service.service_id)}
                  <option value={service.service_id}>{service.name} ({typeLabel(service.service_type)})</option>
                {/each}
              </select>
              <input
                value={form.ip_address}
                on:input={(e) => setAttachField(`s${network.id}`, 'ip_address', e.target.value)}
                placeholder="Address (optional)"
                aria-label="Private address"
              />
              <Button size="small" disabled={working || !form.service_id} on:click={() => submitSharedAttach(network)}>
                Connect
              </Button>
            </div>
          {:else if network.attachments.length === 0}
            <p class="hint">You have no other active services in {network.location_name} to connect.</p>
          {/if}
        {/if}

        {#if sharedDhcp[network.id]}
          {@const dhcp = sharedDhcp[network.id]}
          <div class="sharing">
            <h3>DHCP</h3>
            <p class="hint first">
              {#if !dhcp.enabled}
                The owner has switched DHCP off on this network. Set the address shown inside the operating system by hand.
              {:else if dhcp.served}
                DHCP is on and being served. These are the settings the network owner has chosen; they only apply to machines
                that use DHCP.
              {:else}
                DHCP is on, but is not being served right now: the location may have no storage node yet, or its storage
                nodes are being upgraded or are down. Until it is, set the address shown by hand.
              {/if}
            </p>
            {#if dhcp.enabled}
              <dl class="settings">
                <dt>Lease time</dt><dd>{leaseText(dhcp.lease_seconds)}</dd>
                {#if dhcp.gateway}<dt>Gateway</dt><dd class="mono">{dhcp.gateway}</dd>{/if}
                {#if dhcp.dns_servers?.length}<dt>DNS servers</dt><dd class="mono">{list(dhcp.dns_servers)}</dd>{/if}
                {#if dhcp.domain}<dt>Search domain</dt><dd>{dhcp.domain}</dd>{/if}
                {#if dhcp.mtu}<dt>MTU</dt><dd>{dhcp.mtu}</dd>{/if}
                {#if dhcp.ntp_servers?.length}<dt>NTP servers</dt><dd class="mono">{list(dhcp.ntp_servers)}</dd>{/if}
                {#if dhcp.routes?.length}
                  <dt>Static routes</dt>
                  <dd class="mono">
                    {#each dhcp.routes as route}<div>{route.destination} via {route.gateway}</div>{/each}
                  </dd>
                {/if}
              </dl>
            {/if}
          </div>
        {/if}

        {#if storage.linked}
          <div class="sharing">
            <h3>Storage on this network</h3>
            <p class="hint">
              Storage address <span class="mono">{storage.endpoint_ip}</span>. These shares are open to every server on the network.
              Mount them from a server you have connected above.
            </p>
            {#each storage.shares as share (share.id)}
              <div class="mount">
                <div><strong>{share.name}</strong> {#if share.read_only}<small>read only</small>{/if}</div>
                <code>{share.mount_command}</code>
                <button class="link plain" on:click={() => copy(share.mount_command)}>Copy</button>
                <code>{share.fstab_line}</code>
                <button class="link plain" on:click={() => copy(share.fstab_line)}>Copy</button>
              </div>
            {:else}
              <p class="none">No shares are open to the whole network.</p>
            {/each}
          </div>
        {/if}
      </article>
    {/each}
  {/if}
</section>

<style>
  .page-head {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 16px;
    margin-bottom: 20px;
  }
  .page-head h1 {
    margin: 0;
    font-size: 26px;
    font-weight: 750;
    letter-spacing: -0.02em;
  }
  .sub {
    margin: 4px 0 0;
    font-size: 14px;
    color: var(--text-tertiary);
  }
  .state { min-height: 200px; display: grid; place-items: center; gap: 12px; }
  .panel {
    border: 1px solid var(--border-color);
    border-radius: var(--radius-lg);
    background: var(--portal-card-bg, var(--bg-primary));
    box-shadow: var(--shadow-sm);
    padding: 20px;
    margin-bottom: 16px;
  }
  .panel h2 { margin: 0 0 4px; font-size: 18px; }
  .head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; margin-bottom: 14px; }
  .head p { margin: 0; color: var(--text-secondary); font-size: 13px; }
  .rows { border: 1px solid var(--border-color); border-radius: 8px; overflow-x: auto; }
  .row {
    display: grid;
    grid-template-columns: 1.4fr 1.2fr 0.8fr 0.6fr;
    gap: 12px;
    align-items: center;
    padding: 10px 12px;
    border-bottom: 1px solid var(--border-color);
    font-size: 14px;
    min-width: 560px;
  }
  .row:last-child { border-bottom: 0; }
  .row small { display: block; margin-top: 2px; font-size: 12px; color: var(--text-tertiary); }
  .row a { color: var(--portal-accent); font-weight: 600; text-decoration: none; }
  .row-end { text-align: right; }
  .sharing { margin-top: 18px; padding-top: 14px; border-top: 1px solid var(--border-color); }
  .sharing h3 { margin: 0 0 4px; font-size: 15px; }
  .section { margin: 24px 0 12px; font-size: 20px; }
  .invite { display: grid; grid-template-columns: 1fr auto; gap: 8px; align-items: center; margin-top: 14px; }
  .mount { display: grid; grid-template-columns: 1fr auto; gap: 4px 10px; align-items: center; margin-top: 10px; font-size: 13px; }
  .mount > div { grid-column: 1 / -1; }
  .mount code { overflow-x: auto; white-space: nowrap; padding: 6px 8px; border-radius: 6px; background: var(--bg-secondary, rgba(127, 127, 127, 0.1)); }
  .link.plain { color: var(--portal-accent); }
  .copy-all { margin-right: 14px; }
  .attach { display: grid; grid-template-columns: 1.4fr 1fr auto; gap: 8px; align-items: center; margin-top: 14px; }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  .badge {
    display: inline-block;
    padding: 2px 9px;
    border-radius: var(--radius-pill);
    border: 1px solid var(--border-color);
    font-size: 12px;
    font-weight: 600;
  }
  .badge.ok { color: var(--success-color, #2e7d32); border-color: var(--success-color, #2e7d32); }
  .badge.warn { color: var(--warning-color, #b7791f); border-color: var(--warning-color, #b7791f); }
  .link { background: none; border: none; padding: 0; color: var(--danger-color); cursor: pointer; font-weight: 600; font-size: 13px; }
  .link:disabled { opacity: 0.5; cursor: default; }
  .hint ul { margin: 6px 0 0; padding-left: 20px; }
  .hint li { margin-bottom: 4px; }
  .hint.first { margin-top: 0; }
  .settings { display: grid; grid-template-columns: max-content 1fr; gap: 6px 16px; margin: 10px 0 0; font-size: 13px; }
  .settings dt { color: var(--text-tertiary); }
  .settings dd { margin: 0; }
  .hint, .none { margin: 12px 0 0; font-size: 13px; color: var(--text-tertiary); line-height: 1.5; }
  .none { margin-top: 0; }
  label { display: grid; gap: 6px; margin: 14px 0; font-size: 13px; font-weight: 600; }
  label small { font-weight: 400; color: var(--text-tertiary); }
  input, select {
    padding: 9px 10px;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background: var(--bg-primary);
    color: var(--text-primary);
    min-width: 0;
  }
  .actions { display: flex; gap: 10px; }
  .empty {
    text-align: center;
    padding: 56px 24px;
    background: var(--portal-card-bg);
    border: 1px dashed var(--border-color);
    border-radius: var(--radius-lg);
    color: var(--text-tertiary);
  }
  .empty h2 { margin: 0 0 6px; font-size: 18px; font-weight: 700; color: var(--text-primary); }
  .empty p { margin: 0; font-size: 14px; }
  @media (max-width: 700px) {
    .attach { grid-template-columns: 1fr; }
    .page-head { flex-direction: column; align-items: flex-start; }
  }
</style>
