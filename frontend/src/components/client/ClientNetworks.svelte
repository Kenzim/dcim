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
  } from '../../lib/api.js';

  const STATE_LABELS = { pending: 'Connecting', active: 'Connected', error: 'Being retried', detaching: 'Disconnecting' };
  const emptyCreateForm = () => ({ location_id: '', name: '', cidr: '' });

  let networks = [];
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
      [networks, options] = await Promise.all([clientListPrivateNetworks(), clientPrivateNetworkOptions()]);
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

  function removeNetwork(network) {
    if (!confirm(`Delete private network ${network.name}?`)) return null;
    return run(() => clientDeletePrivateNetwork(network.id), 'Private network deleted.');
  }

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

    {#if networks.length === 0 && !showCreate}
      <div class="empty">
        <h2>No private networks yet</h2>
        {#if options.locations.length === 0}
          <p>Private networks are available once you have an active virtual machine or dedicated server in a supported location.</p>
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
              {#if network.storage_endpoint_ip}
                · <span class="mono">{network.storage_endpoint_ip}</span> reserved for storage
              {/if}
            </p>
          </div>
          {#if network.status === 'deleting'}
            <span class="badge warn">Deleting</span>
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
          <p class="hint">
            Set each address inside the operating system. On a virtual machine, use the extra network card with the MAC
            address shown; machines installed from our templates usually apply the address on their next reboot. On a dedicated server,
            add a VLAN interface with ID {network.vlan_id} on the main network card and give it the address shown.
          </p>
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
