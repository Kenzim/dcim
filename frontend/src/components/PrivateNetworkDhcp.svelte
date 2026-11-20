<script>
  import { onMount } from 'svelte';
  import { Alert, Button, Spinner } from './ui/index.js';
  import {
    getPrivateNetworkDhcp,
    updatePrivateNetworkDhcp,
    addPrivateNetworkDhcpReservation,
    updatePrivateNetworkDhcpReservation,
    removePrivateNetworkDhcpReservation,
  } from '../lib/api.js';

  export let networkId;
  // True on the staff page: uses the admin routes and names the node that answers the network.
  export let admin = false;

  const MAX_ROUTES = 16;

  let dhcp = null;
  let form = null;
  let loading = true;
  let busy = false;
  let loadError = '';
  let settingsError = '';
  let settingsOk = '';
  let reservationError = '';
  let editId = null;
  let editForm = { mac: '', ip: '', label: '' };
  let addForm = { mac: '', ip: '', label: '' };

  const splitList = (text) => text.split(/[\s,]+/).filter(Boolean);
  const when = (iso) => (iso ? new Date(iso).toLocaleString() : '');

  function formFrom(d) {
    return {
      range_start: d.range_start || '',
      range_end: d.range_end || '',
      lease_seconds: d.lease_seconds_set ?? '',
      gateway: d.gateway || '',
      dns_servers: (d.dns_servers || []).join(', '),
      domain: d.domain || '',
      mtu: d.mtu ?? '',
      routes: (d.routes || []).map((r) => ({ destination: r.destination, gateway: r.gateway })),
      ntp_servers: (d.ntp_servers || []).join(', '),
    };
  }

  function apply(next, { resetForm = false } = {}) {
    dhcp = next;
    if (resetForm || form === null) form = formFrom(next);
  }

  async function load() {
    try {
      apply(await getPrivateNetworkDhcp(networkId, { admin }), { resetForm: true });
    } catch (err) {
      loadError = err.message || 'Failed to load the DHCP settings';
    } finally {
      loading = false;
    }
  }

  $: status = !dhcp
    ? ''
    : !dhcp.available
      ? 'DHCP is not offered in this location yet: there is no storage node here, or the storage nodes are still being upgraded. You can save settings now; they apply once DHCP is offered.'
      : !dhcp.enabled
        ? 'DHCP is switched off for this network.'
        : dhcp.served
          ? admin && dhcp.serving_node
            ? `DHCP is on and served by ${dhcp.serving_node}.`
            : 'DHCP is on and being served.'
          : 'DHCP is on, but no storage node is answering for this network right now.';

  async function call(action, { onError }) {
    busy = true;
    onError('');
    try {
      return await action();
    } catch (err) {
      onError(err.message || 'Request failed');
      return null;
    } finally {
      busy = false;
    }
  }

  const setSettingsError = (message) => {
    settingsError = message;
    if (message) settingsOk = '';
  };
  const setReservationError = (message) => (reservationError = message);

  async function toggleEnabled(event) {
    const checked = event.target.checked;
    settingsOk = '';
    const next = await call(() => updatePrivateNetworkDhcp(networkId, { enabled: checked }, { admin }), {
      onError: setSettingsError,
    });
    if (next) {
      apply(next);
      settingsOk = checked ? 'DHCP switched on.' : 'DHCP switched off.';
    } else {
      event.target.checked = dhcp.enabled;
    }
  }

  function settingsBody() {
    const lease = String(form.lease_seconds).trim();
    const mtu = String(form.mtu).trim();
    if (lease && !/^\d+$/.test(lease)) throw new Error('Lease time must be a whole number of seconds');
    if (mtu && !/^\d+$/.test(mtu)) throw new Error('MTU must be a whole number');
    return {
      range_start: form.range_start.trim() || null,
      range_end: form.range_end.trim() || null,
      lease_seconds: lease ? Number(lease) : null,
      gateway: form.gateway.trim() || null,
      dns_servers: splitList(form.dns_servers),
      domain: form.domain.trim() || null,
      mtu: mtu ? Number(mtu) : null,
      routes: form.routes
        .map((r) => ({ destination: r.destination.trim(), gateway: r.gateway.trim() }))
        .filter((r) => r.destination || r.gateway),
      ntp_servers: splitList(form.ntp_servers),
    };
  }

  async function saveSettings() {
    settingsOk = '';
    let body;
    try {
      body = settingsBody();
    } catch (err) {
      setSettingsError(err.message);
      return;
    }
    if (body.gateway && body.gateway !== dhcp.gateway) {
      const warning =
        `Hand out ${body.gateway} as the gateway?\n\nAny machine that uses DHCP on this network card will use it as its ` +
        'default route, which can cut off its internet access if the gateway does not route traffic for it.';
      if (!confirm(warning)) return;
    }
    const next = await call(() => updatePrivateNetworkDhcp(networkId, body, { admin }), { onError: setSettingsError });
    if (next) {
      apply(next, { resetForm: true });
      settingsOk = 'DHCP settings saved.';
    }
  }

  async function removeRange() {
    if (dhcp.range_start || dhcp.range_end) {
      const next = await call(
        () => updatePrivateNetworkDhcp(networkId, { range_start: null, range_end: null }, { admin }),
        { onError: setSettingsError },
      );
      if (!next) return;
      apply(next);
      settingsOk = 'Dynamic range removed.';
    }
    form.range_start = '';
    form.range_end = '';
  }

  function discard() {
    form = formFrom(dhcp);
    settingsError = '';
    settingsOk = '';
  }

  function addRoute() {
    form.routes = [...form.routes, { destination: '', gateway: '' }];
  }

  function dropRoute(index) {
    form.routes = form.routes.filter((_, i) => i !== index);
  }

  async function addReservation() {
    const next = await call(
      () =>
        addPrivateNetworkDhcpReservation(
          networkId,
          { mac: addForm.mac.trim(), ip: addForm.ip.trim(), label: addForm.label.trim() || null },
          { admin },
        ),
      { onError: setReservationError },
    );
    if (next) {
      apply(next);
      addForm = { mac: '', ip: '', label: '' };
    }
  }

  function startEdit(reservation) {
    editId = reservation.id;
    editForm = { mac: reservation.mac, ip: reservation.ip, label: reservation.label || '' };
    reservationError = '';
  }

  async function saveEdit(reservation) {
    const next = await call(
      () =>
        updatePrivateNetworkDhcpReservation(
          networkId,
          reservation.id,
          { mac: editForm.mac.trim(), ip: editForm.ip.trim(), label: editForm.label.trim() || null },
          { admin },
        ),
      { onError: setReservationError },
    );
    if (next) {
      apply(next);
      editId = null;
    }
  }

  async function removeReservation(reservation) {
    if (!confirm(`Remove the reservation of ${reservation.ip} for ${reservation.mac}?`)) return;
    const next = await call(() => removePrivateNetworkDhcpReservation(networkId, reservation.id, { admin }), {
      onError: setReservationError,
    });
    if (next) {
      apply(next);
      if (editId === reservation.id) editId = null;
    }
  }

  onMount(load);
</script>

<div class="dhcp">
  <h3>DHCP</h3>
  {#if loading}
    <div class="loading"><Spinner /><span>Loading…</span></div>
  {:else if loadError}
    <p class="msg bad">{loadError}</p>
  {:else if dhcp}
    <p class="hint first">
      DHCP gives your machines their private address automatically. Every machine connected here is always given the
      address shown above, matched by its MAC address, and nothing else unless you add the settings below.
    </p>

    <div class="switch-line">
      <label class="switch">
        <input type="checkbox" checked={dhcp.enabled} disabled={busy} on:change={toggleEnabled} />
        <span>DHCP {dhcp.enabled ? 'on' : 'off'}</span>
      </label>
      <span class="status" class:ok={dhcp.available && dhcp.enabled && dhcp.served}>{status}</span>
    </div>

    {#if dhcp.error}<Alert type="danger">{dhcp.error}</Alert>{/if}
    {#each dhcp.warnings || [] as warning}<Alert type="warning">{warning}</Alert>{/each}

    <form class="settings" on:submit|preventDefault={saveSettings}>
      <fieldset disabled={busy}>
        <div class="grid">
          <label>Dynamic range start
            <input class="mono" bind:value={form.range_start} placeholder="10.0.0.100" />
          </label>
          <label>Dynamic range end
            <input class="mono" bind:value={form.range_end} placeholder="10.0.0.199" />
          </label>
        </div>
        <p class="hint">
          Optional. Machines that are not connected here or reserved below get an address from this range. Give both ends or
          neither. Addresses in the range are never handed to services you connect.
          {#if form.range_start || form.range_end}
            <button type="button" class="link" on:click={removeRange}>Remove range</button>
          {/if}
        </p>

        <div class="grid">
          <label>Lease time (seconds)
            <input bind:value={form.lease_seconds} inputmode="numeric" placeholder={String(dhcp.lease_seconds_default)} />
            <small>Blank uses the default, {dhcp.lease_seconds_default} seconds. Between 300 and 604800.</small>
          </label>
          <label>MTU
            <input bind:value={form.mtu} inputmode="numeric" placeholder="Not set" />
            <small>Optional, between 576 and 9000.</small>
          </label>
        </div>

        <label>Gateway
          <input class="mono" bind:value={form.gateway} placeholder="Not set" />
        </label>
        <p class="hint warn-text">
          Setting a gateway changes the default route of any machine that uses DHCP on this network card: it will send its
          internet traffic to the gateway instead of its normal route. Only set one if that address is a router on this
          network that you want those machines to use.
        </p>

        <label>DNS servers
          <input class="mono" bind:value={form.dns_servers} placeholder="Not set" />
          <small>Up to 3 addresses, separated by commas.</small>
        </label>
        <label>Search domain
          <input bind:value={form.domain} placeholder="example.internal" />
        </label>
        <label>NTP servers
          <input class="mono" bind:value={form.ntp_servers} placeholder="Not set" />
          <small>Up to 3 addresses, separated by commas.</small>
        </label>

        <div class="routes">
          <span class="legend">Static routes</span>
          <p class="hint first">
            Send traffic for specific subnets through a router on this network, without setting a default gateway. Up to
            {MAX_ROUTES} routes; the router must be an address inside this network.
          </p>
          {#each form.routes as route, index}
            <div class="route">
              <input class="mono" bind:value={route.destination} placeholder="192.168.50.0/24" aria-label="Destination subnet" />
              <input class="mono" bind:value={route.gateway} placeholder="Router, for example 10.0.0.5" aria-label="Router address" />
              <button type="button" class="link" on:click={() => dropRoute(index)}>Remove</button>
            </div>
          {/each}
          {#if form.routes.length < MAX_ROUTES}
            <button type="button" class="link accent" on:click={addRoute}>Add a route</button>
          {/if}
        </div>
      </fieldset>

      {#if settingsError}<p class="msg bad">{settingsError}</p>{/if}
      {#if settingsOk}<p class="msg good">{settingsOk}</p>{/if}
      <div class="actions">
        <Button type="submit" size="small" disabled={busy}>Save settings</Button>
        <Button variant="secondary" size="small" disabled={busy} on:click={discard}>Discard changes</Button>
      </div>
    </form>

    <h4>Reservations</h4>
    <p class="hint first">
      Reserve an address for a machine Rackflow does not manage, such as a router, an appliance, or a dedicated server's own
      interface that has its own MAC address. It always gets that address by DHCP. Machines you connect above are
      handled automatically and do not need one.
    </p>
    {#if dhcp.reservations.length > 0}
      <div class="table-wrap">
        <table>
          <thead><tr><th>MAC address</th><th>Address</th><th>Label</th><th></th></tr></thead>
          <tbody>
            {#each dhcp.reservations as reservation (reservation.id)}
              <tr>
                {#if editId === reservation.id}
                  <td><input class="mono" bind:value={editForm.mac} aria-label="MAC address" /></td>
                  <td><input class="mono" bind:value={editForm.ip} aria-label="Address" /></td>
                  <td><input bind:value={editForm.label} maxlength="64" aria-label="Label" /></td>
                  <td class="end">
                    <button class="link accent" disabled={busy} on:click={() => saveEdit(reservation)}>Save</button>
                    <button class="link plain" disabled={busy} on:click={() => (editId = null)}>Cancel</button>
                  </td>
                {:else}
                  <td class="mono">{reservation.mac}</td>
                  <td class="mono">{reservation.ip}</td>
                  <td>{reservation.label || ''}</td>
                  <td class="end">
                    <button class="link accent" disabled={busy} on:click={() => startEdit(reservation)}>Edit</button>
                    <button class="link" disabled={busy} on:click={() => removeReservation(reservation)}>Remove</button>
                  </td>
                {/if}
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {:else}
      <p class="none">No reservations.</p>
    {/if}
    <div class="add">
      <input class="mono" bind:value={addForm.mac} placeholder="aa:bb:cc:00:00:01" aria-label="MAC address" />
      <input class="mono" bind:value={addForm.ip} placeholder="10.0.0.20" aria-label="Address" />
      <input bind:value={addForm.label} maxlength="64" placeholder="Label (optional)" aria-label="Label" />
      <Button size="small" disabled={busy || !addForm.mac.trim() || !addForm.ip.trim()} on:click={addReservation}>Reserve</Button>
    </div>
    {#if reservationError}<p class="msg bad">{reservationError}</p>{/if}

    {#if dhcp.automatic.length > 0}
      <h4>Automatic</h4>
      <p class="hint first">Addresses Rackflow gives the services connected to this network. These cannot be changed here.</p>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Address</th><th>MAC addresses</th><th>Service</th></tr></thead>
          <tbody>
            {#each dhcp.automatic as item (item.attachment_id)}
              <tr>
                <td class="mono">{item.ip}</td>
                <td class="mono">{(item.macs || []).join(', ')}</td>
                <td>{item.label || ''}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}

    {#if dhcp.leases.length > 0}
      <h4>Dynamic leases</h4>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Address</th><th>MAC address</th><th>Name</th><th>Expires</th></tr></thead>
          <tbody>
            {#each dhcp.leases as lease (lease.mac)}
              <tr>
                <td class="mono">{lease.ip}</td>
                <td class="mono">{lease.mac}</td>
                <td>{lease.hostname || ''}</td>
                <td>{when(lease.expires_at)}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  {/if}
</div>

<style>
  .dhcp { margin-top: 18px; padding-top: 14px; border-top: 1px solid var(--border-color); text-align: left; }
  h3 { margin: 0 0 4px; font-size: 15px; }
  h4 { margin: 18px 0 4px; font-size: 14px; }
  .loading { display: flex; align-items: center; gap: 10px; padding: 12px 0; font-size: 13px; }
  .hint, .none { margin: 8px 0 0; font-size: 13px; color: var(--text-tertiary, var(--text-secondary)); line-height: 1.5; }
  .hint.first { margin-top: 0; }
  .warn-text { color: var(--warning-color, #b7791f); }
  .switch-line { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 16px; margin: 12px 0; }
  .switch { display: flex; align-items: center; gap: 8px; margin: 0; font-size: 14px; font-weight: 600; flex-direction: row; }
  .switch input { width: auto; }
  .status { font-size: 13px; color: var(--text-tertiary, var(--text-secondary)); }
  .status.ok { color: var(--success-color, #2e7d32); }
  fieldset { border: 0; margin: 0; padding: 0; min-width: 0; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  label { display: grid; gap: 6px; margin: 12px 0 0; font-size: 13px; font-weight: 600; }
  label small { font-weight: 400; color: var(--text-tertiary, var(--text-secondary)); }
  input {
    padding: 8px 10px;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background: var(--bg-primary);
    color: var(--text-primary);
    min-width: 0;
    width: 100%;
    box-sizing: border-box;
  }
  .routes { margin-top: 14px; }
  .legend { font-size: 13px; font-weight: 600; }
  .route { display: grid; grid-template-columns: 1fr 1fr auto; gap: 8px; align-items: center; margin-top: 8px; }
  .actions { display: flex; gap: 10px; margin-top: 14px; }
  .add { display: grid; grid-template-columns: 1.2fr 1fr 1fr auto; gap: 8px; align-items: center; margin-top: 10px; }
  .table-wrap { margin-top: 8px; border: 1px solid var(--border-color); border-radius: 8px; overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; min-width: 480px; }
  th { text-align: left; padding: 8px 10px; font-weight: 600; color: var(--text-tertiary, var(--text-secondary)); border-bottom: 1px solid var(--border-color); }
  td { padding: 8px 10px; border-bottom: 1px solid var(--border-color); vertical-align: middle; }
  tr:last-child td { border-bottom: 0; }
  td.end { text-align: right; white-space: nowrap; }
  td.end .link { margin-left: 10px; }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  .link { background: none; border: none; padding: 0; color: var(--danger-color); cursor: pointer; font-weight: 600; font-size: 13px; }
  .link.accent { color: var(--portal-accent, var(--accent-color)); }
  .link.plain { color: var(--text-secondary); }
  .link:disabled { opacity: 0.5; cursor: default; }
  .msg { margin: 10px 0 0; font-size: 13px; }
  .msg.bad { color: var(--danger-color); }
  .msg.good { color: var(--success-color, #2e7d32); }
  @media (max-width: 700px) {
    .grid, .route, .add { grid-template-columns: 1fr; }
  }
</style>
