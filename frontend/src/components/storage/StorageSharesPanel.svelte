<script>
  import { createEventDispatcher } from 'svelte';
  import Button from '../ui/Button.svelte';
  import { createStorageShare, updateStorageShare, deleteStorageShare } from '../../lib/api.js';
  import { GIB, STORAGE_STATUS_LABELS as STATUS_LABELS, formatBytes, percent as pct } from '../../lib/storage.js';

  // A storage object from /api/admin/storage or /api/client/storage.
  export let storage;
  // true: use the client routes; false: the staff routes.
  export let client = false;

  const dispatch = createEventDispatcher();
  let working = false;
  let error = '';
  let copied = '';
  let showCreate = false;
  let form = emptyForm();
  let editingId = null;
  let editForm = {};

  function emptyForm() {
    return { name: '', quota_gb: '', clients: '', all_clients: false, read_only: false };
  }

  $: writable = storage && (storage.status === 'ready' || storage.status === 'provisioning' || storage.status === 'error');
  $: freeGb = storage ? Math.max(0, Math.floor((storage.size_bytes - storage.allocated_bytes) / GIB)) : 0;

  function parseClients(text) {
    return text
      .split(/[\s,]+/)
      .map((s) => s.trim())
      .filter(Boolean);
  }

  async function run(action) {
    working = true;
    error = '';
    try {
      const updated = await action();
      dispatch('change', updated);
      return true;
    } catch (err) {
      error = err.message || 'Request failed';
      return false;
    } finally {
      working = false;
    }
  }

  async function submitCreate() {
    const ok = await run(() =>
      createStorageShare(
        storage.service_id,
        {
          name: form.name.trim(),
          quota_gb: Number(form.quota_gb),
          clients: parseClients(form.clients),
          all_clients: form.all_clients,
          read_only: form.read_only,
        },
        client,
      ),
    );
    if (ok) {
      showCreate = false;
      form = emptyForm();
    }
  }

  function startEdit(share) {
    editingId = share.id;
    editForm = {
      quota_gb: share.quota_gb,
      clients: share.clients.join('\n'),
      all_clients: share.all_clients,
      read_only: share.read_only,
    };
  }

  async function submitEdit(share) {
    const ok = await run(() =>
      updateStorageShare(
        storage.service_id,
        share.id,
        {
          quota_gb: Number(editForm.quota_gb),
          clients: parseClients(editForm.clients),
          all_clients: editForm.all_clients,
          read_only: editForm.read_only,
        },
        client,
      ),
    );
    if (ok) editingId = null;
  }

  function removeShare(share) {
    const message =
      `Delete share "${share.name}"? It stops being exported straight away. ` +
      'Its data is kept for a grace period and then permanently destroyed.';
    if (!confirm(message)) return;
    run(() => deleteStorageShare(storage.service_id, share.id, client));
  }

  async function copy(text, key) {
    try {
      await navigator.clipboard.writeText(text);
      copied = key;
      setTimeout(() => {
        if (copied === key) copied = '';
      }, 1500);
    } catch {
      error = 'Clipboard unavailable';
    }
  }
</script>

<div class="shares">
  <div class="usage">
    <div class="usage-row">
      <span>Allocated to shares</span>
      <span class="mono">{formatBytes(storage.allocated_bytes)} of {formatBytes(storage.size_bytes)}</span>
    </div>
    <div class="bar"><div class="fill alloc" style="width: {pct(storage.allocated_bytes, storage.size_bytes)}%"></div></div>
    <div class="usage-row">
      <span>Used</span>
      <span class="mono">{formatBytes(storage.used_bytes)} of {formatBytes(storage.size_bytes)}</span>
    </div>
    <div class="bar"><div class="fill used" style="width: {pct(storage.used_bytes, storage.size_bytes)}%"></div></div>
  </div>

  {#if error}<div class="banner error">{error}</div>{/if}

  <div class="head">
    <h3>Shares</h3>
    {#if writable && !showCreate}
      <Button size="small" on:click={() => (showCreate = true)} disabled={freeGb < 1}>New share</Button>
    {/if}
  </div>

  {#if showCreate}
    <div class="form">
      <label>Name <input bind:value={form.name} maxlength="32" placeholder="data" /></label>
      <small>Lowercase letters, digits, '-' or '_', starting with a letter. Used in the mount path.</small>
      <label>Quota (GB) <input type="number" min="1" max={freeGb} bind:value={form.quota_gb} /></label>
      <small>{freeGb} GB unallocated.</small>
      <label>Client addresses
        <textarea rows="3" bind:value={form.clients} disabled={form.all_clients} placeholder="10.0.0.5&#10;10.0.0.6"></textarea>
      </label>
      <small>Addresses on {storage.network?.cidr || 'the network'}, one per line or comma separated.</small>
      <label class="check"><input type="checkbox" bind:checked={form.all_clients} /> Allow every service attached to this network</label>
      <label class="check"><input type="checkbox" bind:checked={form.read_only} /> Read only</label>
      <div class="actions">
        <Button size="small" on:click={submitCreate} disabled={working || !form.name.trim() || !form.quota_gb}>Create share</Button>
        <Button size="small" variant="secondary" on:click={() => (showCreate = false)}>Cancel</Button>
      </div>
    </div>
  {/if}

  {#if storage.shares.length === 0}
    <p class="muted">No shares yet.</p>
  {/if}

  {#each storage.shares as share (share.id)}
    <div class="share" class:deleting={share.state === 'deleting'}>
      <div class="share-head">
        <div>
          <strong class="mono">{share.name}</strong>
          <span class="badge" class:ok={share.status === 'ready'} class:bad={share.status === 'error'}>
            {STATUS_LABELS[share.status] || share.status}
          </span>
          {#if share.read_only}<span class="badge">Read only</span>{/if}
        </div>
        {#if share.state === 'active' && writable}
          <div class="actions">
            <button class="tiny" disabled={working} on:click={() => startEdit(share)}>Edit</button>
            <button class="tiny danger" disabled={working} on:click={() => removeShare(share)}>Delete</button>
          </div>
        {/if}
      </div>

      {#if share.state === 'deleting'}
        <p class="muted">Deleted. The data is destroyed after {share.destroy_after ? new Date(share.destroy_after).toLocaleString() : 'the grace period'}.</p>
      {:else}
        <div class="usage-row">
          <span>Used</span>
          <span class="mono">{formatBytes(share.used_bytes)} of {formatBytes(share.quota_bytes)}</span>
        </div>
        <div class="bar"><div class="fill used" style="width: {pct(share.used_bytes, share.quota_bytes)}%"></div></div>
        <p class="clients">
          Clients:
          {#if share.all_clients}
            every attached service
            {#if share.effective_clients.length}(<span class="mono">{share.effective_clients.join(', ')}</span>){/if}
          {:else if share.clients.length}
            <span class="mono">{share.clients.join(', ')}</span>
          {:else}
            none yet
          {/if}
        </p>
        {#if share.runner_error}<p class="err">Runner: {share.runner_error}</p>{/if}

        {#if editingId === share.id}
          <div class="form">
            <label>Quota (GB) <input type="number" min="1" bind:value={editForm.quota_gb} /></label>
            <label>Client addresses
              <textarea rows="3" bind:value={editForm.clients} disabled={editForm.all_clients}></textarea>
            </label>
            <label class="check"><input type="checkbox" bind:checked={editForm.all_clients} /> Allow every service attached to this network</label>
            <label class="check"><input type="checkbox" bind:checked={editForm.read_only} /> Read only</label>
            <div class="actions">
              <Button size="small" on:click={() => submitEdit(share)} disabled={working}>Save</Button>
              <Button size="small" variant="secondary" on:click={() => (editingId = null)}>Cancel</Button>
            </div>
          </div>
        {/if}

        {#if share.mount_command}
          <div class="copyline">
            <code>{share.mount_command}</code>
            <button class="tiny" on:click={() => copy(share.mount_command, `m${share.id}`)}>{copied === `m${share.id}` ? 'Copied' : 'Copy'}</button>
          </div>
          <div class="copyline">
            <code>{share.fstab_line}</code>
            <button class="tiny" on:click={() => copy(share.fstab_line, `f${share.id}`)}>{copied === `f${share.id}` ? 'Copied' : 'Copy'}</button>
          </div>
        {/if}
      {/if}
    </div>
  {/each}
</div>

<style>
  .shares { display: flex; flex-direction: column; gap: 10px; }
  .head, .share-head { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
  h3 { margin: 6px 0 0; font-size: 15px; }
  .usage-row { display: flex; justify-content: space-between; font-size: 13px; color: var(--text-secondary); margin-top: 4px; }
  .bar { height: 8px; border-radius: 4px; background: var(--bg-tertiary, var(--bg-secondary)); overflow: hidden; }
  .fill { height: 100%; }
  .fill.alloc { background: var(--accent-color, #3b82f6); opacity: 0.55; }
  .fill.used { background: var(--accent-color, #3b82f6); }
  .share { border: 1px solid var(--border-color); border-radius: 8px; padding: 12px; display: flex; flex-direction: column; gap: 6px; }
  .share.deleting { opacity: 0.7; }
  .mono, code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.85rem; }
  .badge { display: inline-block; margin-left: 6px; padding: 1px 8px; border-radius: 999px; font-size: 12px; font-weight: 600; border: 1px solid var(--border-color); color: var(--text-secondary); }
  .badge.ok { color: var(--success-text, #2e7d32); border-color: currentColor; }
  .badge.bad { color: var(--danger-color, #c62828); border-color: currentColor; }
  .clients, .muted { margin: 0; font-size: 13px; color: var(--text-secondary); }
  .err { margin: 0; font-size: 13px; color: var(--danger-color); }
  .copyline { display: flex; gap: 8px; align-items: center; background: var(--bg-secondary); border-radius: 6px; padding: 6px 8px; }
  .copyline code { flex: 1; overflow-x: auto; white-space: nowrap; }
  .form { display: flex; flex-direction: column; gap: 6px; border: 1px dashed var(--border-color); border-radius: 8px; padding: 12px; }
  .form label { display: grid; gap: 4px; font-size: 13px; font-weight: 600; }
  .form label.check { display: flex; align-items: center; gap: 6px; font-weight: 400; }
  .form small { color: var(--text-tertiary); font-size: 12px; margin-top: -4px; }
  input:not([type='checkbox']), textarea { padding: 7px 9px; border: 1px solid var(--border-color); border-radius: 6px; background: var(--bg-primary); color: var(--text-primary); }
  .actions { display: flex; gap: 6px; }
  .tiny { padding: 3px 8px; border: 1px solid var(--border-color); border-radius: 6px; background: var(--bg-primary); color: var(--text-primary); cursor: pointer; font-size: 12px; }
  .tiny.danger { color: var(--danger-color); border-color: var(--danger-color); }
  .tiny:disabled { opacity: 0.5; cursor: default; }
  .banner.error { padding: 8px 10px; border-radius: 6px; background: var(--danger-bg); color: var(--danger-text); }
</style>
