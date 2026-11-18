<script>
  import { onMount, onDestroy } from 'svelte';
  import PageHeader from './PageHeader.svelte';
  import Button from './ui/Button.svelte';
  import Modal from './ui/Modal.svelte';
  import {
    listProxyRunners,
    createProxyRunner,
    updateProxyRunner,
    rotateProxyRunnerKey,
    deleteProxyRunner,
    getLocations,
    getServers,
  } from '../lib/api.js';

  let loading = false;
  let error = '';
  let success = '';
  let runners = [];
  let pollTimer;

  let showCreate = false;
  let createName = '';
  let createBusy = false;

  let revealedKey = null; // { title, api_key }
  let rotateBusyId = null;

  let placing = null; // runner being placed on the proxy fast path
  let placeLocation = '';
  let placeServer = '';
  let placeMode = 'preconfigured';
  let placeBusy = false;
  let locations = [];
  let servers = [];

  async function openPlacement(runner) {
    error = '';
    placing = runner;
    placeLocation = runner.location_id ? String(runner.location_id) : '';
    placeServer = runner.server_id ? String(runner.server_id) : '';
    placeMode = runner.vlan_mode || 'preconfigured';
    try {
      if (!locations.length) locations = await getLocations();
      if (!servers.length) servers = await getServers();
    } catch (err) {
      error = err.message || 'Failed to load locations and servers';
    }
  }

  $: placeServers = servers.filter((s) => !placeLocation || String(s.location_id) === placeLocation);

  async function submitPlacement() {
    try {
      placeBusy = true;
      error = '';
      await updateProxyRunner(placing.id, {
        location_id: placeLocation ? Number(placeLocation) : null,
        server_id: placeServer ? Number(placeServer) : null,
        vlan_mode: placeMode,
      });
      placing = null;
      await load();
    } catch (err) {
      error = err.message || 'Failed to update placement';
    } finally {
      placeBusy = false;
    }
  }

  onMount(async () => {
    await load();
    pollTimer = setInterval(load, 15000);
  });

  onDestroy(() => {
    if (pollTimer) clearInterval(pollTimer);
  });

  async function load() {
    try {
      loading = runners.length === 0;
      error = '';
      runners = await listProxyRunners();
    } catch (err) {
      error = err.message || 'Failed to load proxy runners';
    } finally {
      loading = false;
    }
  }

  function formatSeen(iso) {
    if (!iso) return 'Never';
    try {
      return new Date(iso).toLocaleString();
    } catch {
      return iso;
    }
  }

  async function submitCreate() {
    const name = (createName || '').trim();
    if (!name) {
      error = 'Name is required';
      return;
    }
    try {
      createBusy = true;
      error = '';
      const row = await createProxyRunner({ name });
      showCreate = false;
      createName = '';
      revealedKey = {
        title: `API key for “${row.name}” (shown once)`,
        api_key: row.api_key,
      };
      await load();
    } catch (err) {
      error = err.message || 'Failed to create proxy runner';
    } finally {
      createBusy = false;
    }
  }

  async function toggleEnabled(runner) {
    try {
      error = '';
      await updateProxyRunner(runner.id, { enabled: !runner.enabled });
      await load();
    } catch (err) {
      error = err.message || 'Failed to update runner';
    }
  }

  async function rotateKey(runner) {
    if (!confirm(`Rotate API key for “${runner.name}”? The old key stops working immediately.`)) return;
    try {
      rotateBusyId = runner.id;
      error = '';
      const res = await rotateProxyRunnerKey(runner.id);
      revealedKey = {
        title: `New API key for “${runner.name}” (shown once)`,
        api_key: res.api_key,
      };
    } catch (err) {
      error = err.message || 'Failed to rotate key';
    } finally {
      rotateBusyId = null;
    }
  }

  async function removeRunner(runner) {
    if (!confirm(`Delete proxy runner “${runner.name}”?`)) return;
    try {
      error = '';
      await deleteProxyRunner(runner.id);
      success = 'Proxy runner deleted';
      setTimeout(() => { if (success === 'Proxy runner deleted') success = ''; }, 2000);
      await load();
    } catch (err) {
      error = err.message || 'Failed to delete runner';
    }
  }

  async function copyKey() {
    if (!revealedKey?.api_key) return;
    try {
      await navigator.clipboard.writeText(revealedKey.api_key);
      success = 'API key copied';
      setTimeout(() => { if (success === 'API key copied') success = ''; }, 2000);
    } catch {
      error = 'Clipboard unavailable';
    }
  }
</script>

<PageHeader title="Proxy Runners" />

<div class="page">
  <p class="intro">
    Register standalone proxy runners and generate API keys. Each runner phones home every 30s via
    <code>GET /api/runner/proxy/config</code>; status is Online when last seen within 90 seconds.
    Set <code>RUNNER_API_KEY</code> on the Go proxy runner. Every runner serves every published proxy IP.
  </p>
  <p class="intro">
    <strong>Proxy fast path:</strong> give a runner a location to let clients there reach it over their private
    access network. Pin each proxy subnet to the runner that hosts it in Proxy IPAM; the subnet's private IP then
    lives on that runner. Runners without a location are not part of the fast path.
  </p>

  {#if error}
    <div class="banner error">{error}</div>
  {/if}
  {#if success}
    <div class="banner ok">{success}</div>
  {/if}

  <div class="toolbar">
    <Button size="small" on:click={() => { showCreate = true; error = ''; }}>Add proxy runner</Button>
    <Button variant="secondary" size="small" on:click={load} disabled={loading}>Refresh</Button>
  </div>

  {#if loading}
    <div class="muted">Loading…</div>
  {:else}
    <div class="table">
      <div class="row head">
        <div>Name</div>
        <div>Status</div>
        <div>Last seen</div>
        <div>Last IP</div>
        <div>Enabled</div>
        <div>Fast path</div>
        <div>Actions</div>
      </div>
      {#each runners as runner}
        <div class="row">
          <div>{runner.name}</div>
          <div>
            <span class="badge" class:online={runner.online && runner.enabled} class:offline={!runner.online || !runner.enabled}>
              {#if !runner.enabled}
                Disabled
              {:else if runner.online}
                Online
              {:else}
                Offline
              {/if}
            </span>
          </div>
          <div class="mono">{formatSeen(runner.last_seen_at)}</div>
          <div class="mono">{runner.last_seen_ip || '—'}</div>
          <div>{runner.enabled ? 'yes' : 'no'}</div>
          <div class="fast">
            {#if runner.location_id}
              <div>{runner.location_name} · {runner.vlan_mode === 'switch' ? `switch (${runner.server_name || 'no server'})` : 'preconfigured VLANs'}</div>
              {#each runner.hosted_subnets || [] as subnet}
                <div class="mono">{subnet.cidr} → {subnet.fast_path_ip}:{runner.fast_path_port}</div>
              {:else}
                <div class="mono">No subnets pinned</div>
              {/each}
            {:else}
              <span class="mono">Not placed</span>
            {/if}
          </div>
          <div class="actions">
            <button class="tiny" on:click={() => openPlacement(runner)}>Placement</button>
            <button class="tiny" on:click={() => toggleEnabled(runner)}>
              {runner.enabled ? 'Disable' : 'Enable'}
            </button>
            <button class="tiny" disabled={rotateBusyId === runner.id} on:click={() => rotateKey(runner)}>
              {rotateBusyId === runner.id ? 'Rotating…' : 'Rotate key'}
            </button>
            <button class="tiny danger" on:click={() => removeRunner(runner)}>Delete</button>
          </div>
        </div>
      {:else}
        <div class="empty">No proxy runners yet.</div>
      {/each}
    </div>
  {/if}
</div>

{#if showCreate}
  <Modal title="Add proxy runner" onClose={() => (showCreate = false)}>
    <div class="form">
      <label for="pr-name">Name</label>
      <input id="pr-name" type="text" bind:value={createName} placeholder="e.g. London proxy fleet" />
      <div class="form-actions">
        <Button size="small" on:click={submitCreate} disabled={createBusy}>
          {createBusy ? 'Creating…' : 'Create & generate key'}
        </Button>
        <Button variant="secondary" size="small" on:click={() => (showCreate = false)}>Cancel</Button>
      </div>
    </div>
  </Modal>
{/if}

{#if placing}
  <Modal title={`Fast path placement for “${placing.name}”`} onClose={() => (placing = null)}>
    <div class="form">
      <label for="pr-loc">Location</label>
      <select id="pr-loc" bind:value={placeLocation}>
        <option value="">None (not part of the fast path)</option>
        {#each locations as loc}
          <option value={String(loc.id)}>{loc.name}</option>
        {/each}
      </select>
      <label for="pr-mode">VLAN mode</label>
      <select id="pr-mode" bind:value={placeMode}>
        <option value="preconfigured">Preconfigured: the pool's VLAN range is already carried to the runner</option>
        <option value="switch">Switch: Rackflow trunks each VLAN on the runner server's ports</option>
      </select>
      <label for="pr-srv">Server the runner runs on</label>
      <select id="pr-srv" bind:value={placeServer}>
        <option value="">None</option>
        {#each placeServers as srv}
          <option value={String(srv.id)}>{srv.name}</option>
        {/each}
      </select>
      <p class="hint">Switch mode needs the server, cabled to a switch with a VLAN plugin.</p>
      <div class="form-actions">
        <Button size="small" on:click={submitPlacement} disabled={placeBusy}>{placeBusy ? 'Saving…' : 'Save'}</Button>
        <Button variant="secondary" size="small" on:click={() => (placing = null)}>Cancel</Button>
      </div>
    </div>
  </Modal>
{/if}

{#if revealedKey}
  <Modal title={revealedKey.title} onClose={() => (revealedKey = null)}>
    <p class="warn">Copy this key now. It will not be shown again.</p>
    <pre class="key-box">{revealedKey.api_key}</pre>
    <div class="form-actions">
      <Button size="small" on:click={copyKey}>Copy</Button>
      <Button variant="secondary" size="small" on:click={() => (revealedKey = null)}>Done</Button>
    </div>
  </Modal>
{/if}

<style>
  .page {
    padding: 32px;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .intro {
    color: var(--text-secondary);
    margin: 0;
    max-width: 52rem;
    line-height: 1.45;
  }
  .intro code {
    font-size: 0.9em;
    color: var(--text-primary);
  }
  .toolbar {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }
  .banner {
    padding: 0.65rem 0.85rem;
    border-radius: 6px;
  }
  .banner.error {
    background: var(--danger-bg);
    color: var(--danger-text);
  }
  .banner.ok {
    background: var(--success-bg);
    color: var(--success-text);
  }
  .table {
    display: flex;
    flex-direction: column;
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: 10px;
    overflow-x: auto;
  }
  .row {
    display: grid;
    grid-template-columns: minmax(140px, 1.4fr) 110px minmax(140px, 1.2fr) minmax(100px, 0.9fr) 70px minmax(180px, 1.4fr) minmax(200px, 1.2fr);
    gap: 10px;
    padding: 12px 14px;
    align-items: center;
    border-bottom: 1px solid var(--border-color);
    font-size: 0.9rem;
    color: var(--text-primary);
  }
  .row:last-child {
    border-bottom: none;
  }
  .row.head {
    font-weight: 600;
    font-size: 0.8rem;
    color: var(--text-secondary);
    background: var(--bg-secondary);
  }
  .mono {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.85rem;
    color: var(--text-secondary);
    word-break: break-all;
  }
  .badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 999px;
    font-size: 0.8rem;
    font-weight: 600;
    border: 1px solid var(--border-color);
    background: var(--bg-secondary);
    color: var(--text-secondary);
  }
  .badge.online {
    background: var(--success-bg);
    color: var(--success-text);
    border-color: transparent;
  }
  .badge.offline {
    background: var(--bg-tertiary);
    color: var(--text-secondary);
  }
  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  .tiny {
    width: fit-content;
    padding: 4px 8px;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background: var(--bg-secondary);
    color: var(--text-primary);
    cursor: pointer;
    font-size: 0.8rem;
  }
  .tiny:hover {
    border-color: var(--accent-color);
  }
  .tiny.danger {
    color: var(--danger-color);
    border-color: var(--danger-color);
  }
  .tiny:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .empty, .muted {
    padding: 16px 14px;
    color: var(--text-secondary);
  }
  .form {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }
  .form label {
    font-weight: 600;
    font-size: 0.9rem;
    color: var(--text-primary);
  }
  .fast {
    display: flex;
    flex-direction: column;
    gap: 2px;
    font-size: 0.85rem;
  }
  .hint {
    margin: 0;
    font-size: 0.8rem;
    color: var(--text-secondary);
  }
  .form input, .form select {
    padding: 8px;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background: var(--bg-secondary);
    color: var(--text-primary);
  }
  .form-actions {
    display: flex;
    gap: 8px;
    margin-top: 0.75rem;
  }
  .warn {
    color: var(--warning-text);
    margin: 0 0 0.75rem;
  }
  .key-box {
    margin: 0;
    padding: 0.75rem;
    background: var(--bg-secondary);
    border: 1px solid var(--border-color);
    color: var(--text-primary);
    border-radius: 6px;
    overflow-x: auto;
    word-break: break-all;
    white-space: pre-wrap;
    font-size: 0.85rem;
  }
</style>
