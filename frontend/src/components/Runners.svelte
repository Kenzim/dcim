<script>
  import { onMount, onDestroy } from 'svelte';
  import PageHeader from './PageHeader.svelte';
  import Button from './ui/Button.svelte';
  import Modal from './ui/Modal.svelte';
  import {
    listRunners,
    createRunner,
    updateRunner,
    rotateRunnerKey,
    deleteRunner,
    getLocations,
  } from '../lib/api.js';

  let loading = false;
  let error = '';
  let success = '';
  let runners = [];
  let locations = [];
  let pollTimer;

  let showCreate = false;
  let createName = '';
  let createLocationId = '';
  let createCaps = { dhcp: false, tftp: false, media: true };
  let createBusy = false;

  let revealedKey = null;
  let rotateBusyId = null;

  onMount(async () => {
    await Promise.all([load(), loadLocations()]);
    pollTimer = setInterval(load, 4000);
  });

  onDestroy(() => {
    if (pollTimer) clearInterval(pollTimer);
  });

  async function loadLocations() {
    try {
      locations = await getLocations();
    } catch {
      locations = [];
    }
  }

  async function load() {
    try {
      loading = runners.length === 0;
      error = '';
      runners = await listRunners();
    } catch (err) {
      error = err.message || 'Failed to load runners';
    } finally {
      loading = false;
    }
  }

  function locationName(id) {
    if (!id) return 'Unassigned';
    const loc = locations.find((row) => row.id === id);
    return loc ? loc.name : `#${id}`;
  }

  function formatSeen(iso) {
    if (!iso) return 'Never';
    try {
      return new Date(iso).toLocaleString();
    } catch {
      return iso;
    }
  }

  function capsLabel(caps) {
    return (caps || []).join(', ') || '—';
  }

  async function submitCreate() {
    const name = (createName || '').trim();
    if (!name) {
      error = 'Name is required';
      return;
    }
    const capabilities = Object.keys(createCaps).filter((key) => createCaps[key]);
    if (!capabilities.length) {
      error = 'Select at least one capability';
      return;
    }
    try {
      createBusy = true;
      error = '';
      const payload = { name, capabilities };
      if (createLocationId) payload.location_id = Number(createLocationId);
      const row = await createRunner(payload);
      showCreate = false;
      createName = '';
      createLocationId = '';
      createCaps = { dhcp: false, tftp: false, media: true };
      revealedKey = {
        title: `API key for “${row.name}” (shown once)`,
        api_key: row.api_key,
        env: `RACKFLOW_URL=https://your-rackflow-host\nAPI_KEY=${row.api_key}`,
      };
      await load();
    } catch (err) {
      error = err.message || 'Failed to create runner';
    } finally {
      createBusy = false;
    }
  }

  async function toggleEnabled(runner) {
    try {
      error = '';
      await updateRunner(runner.id, { enabled: !runner.enabled });
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
      const res = await rotateRunnerKey(runner.id);
      revealedKey = {
        title: `New API key for “${runner.name}” (shown once)`,
        api_key: res.api_key,
        env: `RACKFLOW_URL=https://your-rackflow-host\nAPI_KEY=${res.api_key}`,
      };
    } catch (err) {
      error = err.message || 'Failed to rotate key';
    } finally {
      rotateBusyId = null;
    }
  }

  async function removeRunner(runner) {
    if (!confirm(`Delete runner “${runner.name}”?`)) return;
    try {
      error = '';
      await deleteRunner(runner.id);
      success = 'Runner deleted';
      setTimeout(() => { if (success === 'Runner deleted') success = ''; }, 2000);
      await load();
    } catch (err) {
      error = err.message || 'Failed to delete runner';
    }
  }

  async function copyText(value) {
    try {
      await navigator.clipboard.writeText(value);
      success = 'Copied';
      setTimeout(() => { if (success === 'Copied') success = ''; }, 2000);
    } catch {
      error = 'Clipboard unavailable';
    }
  }
</script>

<PageHeader title="Runners" />

<div class="page">
  <p class="intro">
    Enroll DHCP, TFTP, and ISO/media runners. Each container dials
    <code>wss://…/api/runner/ws</code> with the generated API key, pushes live state,
    and receives commands over the same socket. Status on this page is cache-only
    (online vs daemon running).
  </p>

  {#if error}
    <div class="banner error">{error}</div>
  {/if}
  {#if success}
    <div class="banner ok">{success}</div>
  {/if}

  <div class="toolbar">
    <Button size="small" on:click={() => { showCreate = true; error = ''; }}>Add runner</Button>
    <Button variant="secondary" size="small" on:click={load} disabled={loading}>Refresh</Button>
  </div>

  {#if loading}
    <div class="muted">Loading…</div>
  {:else}
    <div class="table">
      <div class="row head">
        <div>Name</div>
        <div>Location</div>
        <div>Capabilities</div>
        <div>Link</div>
        <div>Daemon</div>
        <div>Last seen</div>
        <div>Actions</div>
      </div>
      {#each runners as runner}
        <div class="row">
          <div>{runner.name}</div>
          <div>{locationName(runner.location_id)}</div>
          <div class="mono">{capsLabel(runner.capabilities)}</div>
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
          <div>
            {#if runner.online && runner.running}
              Running
            {:else if runner.online && runner.stale}
              Stale
            {:else if runner.online}
              Stopped
            {:else}
              —
            {/if}
          </div>
          <div class="mono">{formatSeen(runner.last_seen_at)}</div>
          <div class="actions">
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
        <div class="empty">No runners yet. Enroll one and set <code>RACKFLOW_URL</code> + <code>API_KEY</code> on the container.</div>
      {/each}
    </div>
  {/if}
</div>

{#if showCreate}
  <Modal title="Add runner" onClose={() => (showCreate = false)}>
    <div class="form">
      <label for="rn-name">Name</label>
      <input id="rn-name" type="text" bind:value={createName} placeholder="e.g. London media" />
      <label for="rn-loc">Location</label>
      <select id="rn-loc" bind:value={createLocationId}>
        <option value="">Unassigned</option>
        {#each locations as loc}
          <option value={loc.id}>{loc.name}</option>
        {/each}
      </select>
      <fieldset>
        <legend>Capabilities</legend>
        <label class="check"><input type="checkbox" bind:checked={createCaps.dhcp} /> dhcp</label>
        <label class="check"><input type="checkbox" bind:checked={createCaps.tftp} /> tftp</label>
        <label class="check"><input type="checkbox" bind:checked={createCaps.media} /> media (ISO HTTP + SMB)</label>
      </fieldset>
      <div class="form-actions">
        <Button size="small" on:click={submitCreate} disabled={createBusy}>
          {createBusy ? 'Creating…' : 'Create & generate key'}
        </Button>
        <Button variant="secondary" size="small" on:click={() => (showCreate = false)}>Cancel</Button>
      </div>
    </div>
  </Modal>
{/if}

{#if revealedKey}
  <Modal title={revealedKey.title} onClose={() => (revealedKey = null)}>
    <p class="warn">Copy this key now. It will not be shown again. Set it on the runner container:</p>
    <pre class="key-box">{revealedKey.env}</pre>
    <div class="form-actions">
      <Button size="small" on:click={() => copyText(revealedKey.api_key)}>Copy key</Button>
      <Button variant="secondary" size="small" on:click={() => copyText(revealedKey.env)}>Copy env</Button>
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
    grid-template-columns: minmax(120px, 1.2fr) minmax(110px, 1fr) minmax(110px, 1fr) 90px 80px minmax(130px, 1fr) minmax(200px, 1.2fr);
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
  .form label, .form legend {
    font-weight: 600;
    font-size: 0.9rem;
    color: var(--text-primary);
  }
  .form input, .form select {
    padding: 8px;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background: var(--bg-secondary);
    color: var(--text-primary);
  }
  fieldset {
    border: 1px solid var(--border-color);
    border-radius: 6px;
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
  }
  .check {
    font-weight: 400 !important;
    display: flex;
    gap: 0.4rem;
    align-items: center;
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
