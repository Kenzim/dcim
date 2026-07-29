<script>
  import { onDestroy, onMount } from 'svelte';
  import { Alert, Badge, Button, Spinner } from '../ui/index.js';
  import {
    listVmBackups,
    createVmBackup,
    deleteVmBackup,
    restoreVmBackup,
  } from '../../lib/api.js';

  /** VM service object (uses `id` and `proxmox_vmid`). */
  export let service;

  let loading = true;
  let error = '';
  let message = '';
  let notes = '';
  let backups = [];
  let jobs = [];
  let busy = false;
  let pollTimer = null;

  function kindLabel(kind) {
    const k = String(kind || '').toLowerCase();
    if (k === 'platform') return 'Scheduled';
    if (k === 'client') return 'Customer';
    if (k === 'restore' || k === 'backup') return k.charAt(0).toUpperCase() + k.slice(1);
    return kind ? String(kind) : 'Backup';
  }

  function formatTime(ctime) {
    if (!ctime) return '—';
    const ms = Number(ctime) > 1e12 ? Number(ctime) : Number(ctime) * 1000;
    if (!Number.isFinite(ms)) return String(ctime);
    return new Date(ms).toLocaleString();
  }

  function stopPoll() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  function ensurePoll() {
    if (pollTimer) return;
    pollTimer = setInterval(() => load({ background: true }), 10000);
  }

  async function load({ background = false } = {}) {
    if (!background) loading = true;
    try {
      const res = await listVmBackups(service.id, { client: true });
      backups = res.backups || [];
      jobs = res.jobs || [];
      if (jobs.length) ensurePoll();
      else stopPoll();
      if (!background) error = '';
    } catch (e) {
      backups = [];
      jobs = [];
      error = e.message || String(e);
      stopPoll();
    } finally {
      loading = false;
    }
  }

  async function create() {
    if (busy) return;
    busy = true;
    message = '';
    error = '';
    try {
      await createVmBackup(service.id, { notes: notes || undefined, wait: false }, { client: true });
      notes = '';
      message = 'Backup started';
      await load({ background: true });
    } catch (e) {
      error = e.message || String(e);
    } finally {
      busy = false;
    }
  }

  async function remove(b) {
    if (busy || !b?.deletable) return;
    if (!confirm('Delete this client backup?')) return;
    busy = true;
    message = '';
    error = '';
    try {
      await deleteVmBackup(service.id, { volid: b.volid, storage: b.storage }, { client: true });
      message = 'Backup deleted';
      await load({ background: true });
    } catch (e) {
      error = e.message || String(e);
    } finally {
      busy = false;
    }
  }

  async function restore(b) {
    if (busy) return;
    if (!confirm(`Restore this backup onto VMID ${service.proxmox_vmid}? The current guest will be overwritten.`)) return;
    busy = true;
    message = '';
    error = '';
    try {
      await restoreVmBackup(service.id, { volid: b.volid, storage: b.storage, wait: false }, { client: true });
      message = 'Restore started';
      await load({ background: true });
    } catch (e) {
      error = e.message || String(e);
    } finally {
      busy = false;
    }
  }

  onMount(load);
  onDestroy(stopPoll);
</script>

<section class="panel">
  <header class="panel-head">
    <h3>Backups</h3>
    <p class="muted">Scheduled backups are read-only. Customer backups count toward your quota and can be deleted.</p>
  </header>

  {#if loading}
    <div class="loading"><Spinner size="small" /> Loading backups…</div>
  {:else}
    {#if error}<Alert type="error">{error}</Alert>{/if}

    <div class="create-row">
      <input
        type="text"
        placeholder="Backup name / notes (optional)"
        bind:value={notes}
        disabled={busy}
        aria-label="Backup notes"
      />
      <Button variant="secondary" disabled={busy || jobs.some((j) => j.kind === 'backup')} on:click={create}>
        Create backup
      </Button>
    </div>
    {#if message}<p class="ok">{message}</p>{/if}

    {#if jobs.length}
      <div class="jobs">
        <p class="jobs-title">Running jobs — this list refreshes automatically.</p>
        <ul>
          {#each jobs as j}
            <li class="row row-running">
              <Badge variant="warning">{kindLabel(j.scope || j.kind)}</Badge>
              <span>{j.status || 'RUNNING'}</span>
              {#if j.starttime}<span class="muted">{formatTime(j.starttime)}</span>{/if}
              {#if j.storage}<span class="muted">{j.storage}</span>{/if}
            </li>
          {/each}
        </ul>
      </div>
    {/if}

    {#if !backups.length}
      <p class="muted empty">No backups yet.</p>
    {:else}
      <ul class="backup-list">
        {#each backups as b}
          <li class="row" class:row-running={!!b.running}>
            <Badge variant={b.kind === 'platform' ? 'info' : 'accent'}>{kindLabel(b.kind)}</Badge>
            <span class="row-main">
              <span>{formatTime(b.ctime)}</span>
              {#if b.template_name}<span class="template">{b.template_name}</span>{/if}
              {#if b.notes_display}<span class="notes">{b.notes_display}</span>{/if}
              {#if b.storage}<span class="muted">{b.storage}</span>{/if}
            </span>
            {#if b.running}
              <Badge variant="warning">Running</Badge>
            {:else}
              <span class="row-actions">
                <Button variant="secondary" size="small" disabled={busy} on:click={() => restore(b)}>Restore</Button>
                {#if b.deletable}
                  <Button variant="danger" size="small" disabled={busy} on:click={() => remove(b)}>Delete</Button>
                {/if}
              </span>
            {/if}
          </li>
        {/each}
      </ul>
    {/if}
  {/if}
</section>

<style>
  .panel-head h3 {
    margin: 0 0 4px;
    font-size: 16px;
    font-weight: 700;
  }
  .panel-head p {
    margin: 0 0 16px;
  }
  .muted {
    color: var(--text-tertiary);
    font-size: 13px;
  }
  .loading {
    display: flex;
    align-items: center;
    gap: 10px;
    color: var(--text-secondary);
    font-size: 14px;
  }
  .create-row {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 10px;
  }
  .create-row input {
    flex: 1 1 200px;
    padding: 10px 12px;
    border: 2px solid var(--border-color);
    border-radius: 8px;
    font-size: 14px;
    font-family: inherit;
    background: var(--bg-primary);
    color: var(--text-primary);
  }
  .create-row input:focus {
    outline: none;
    border-color: var(--accent-color);
    box-shadow: var(--focus-ring-accent);
  }
  .ok {
    margin: 4px 0 10px;
    font-size: 13px;
    color: var(--success-color);
  }
  .jobs {
    margin-bottom: 14px;
    padding: 12px 14px;
    border: 1px solid var(--warning-color);
    border-radius: var(--radius-md);
    background: var(--warning-bg);
  }
  .jobs-title {
    margin: 0 0 8px;
    font-size: 13px;
    color: var(--warning-text);
  }
  .jobs ul,
  .backup-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    gap: 8px;
  }
  .row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 10px;
    padding: 10px 12px;
    border: 1px solid var(--border-color);
    border-radius: var(--radius-md);
    background: var(--bg-secondary);
    font-size: 13px;
  }
  .row-main {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 10px;
    flex: 1;
    min-width: 0;
  }
  .row-actions {
    display: inline-flex;
    gap: 8px;
    margin-left: auto;
  }
  .template {
    padding: 2px 8px;
    border-radius: var(--radius-sm);
    background: var(--bg-tertiary);
    color: var(--text-secondary);
    font-size: 12px;
    font-weight: 600;
  }
  .notes {
    font-weight: 650;
  }
  .empty {
    margin: 8px 0 0;
  }
</style>
