<script>
  import { onMount } from 'svelte';
  import { Alert, Button, Spinner } from '../ui/index.js';
  import {
    clientListBackupSlots,
    clientReleaseSlotBackup,
    clientRestoreSlotBackup,
  } from '../../lib/api.js';

  let slots = [];
  let targets = {};
  let loading = true;
  let working = false;
  let error = '';
  let success = '';

  const STATUS_LABELS = { active: 'Active', suspended: 'Suspended', terminated: 'Cancelled' };

  function formatTime(value) {
    if (!value) return '-';
    const d = new Date(value);
    return Number.isNaN(d.getTime()) ? String(value) : d.toLocaleString();
  }

  function formatBytes(bytes) {
    if (!bytes) return '-';
    const gb = bytes / 1024 ** 3;
    return gb >= 1 ? `${gb.toFixed(1)} GB` : `${(bytes / 1024 ** 2).toFixed(0)} MB`;
  }

  async function load() {
    try {
      const res = await clientListBackupSlots();
      slots = res.slots || [];
      error = '';
    } catch (err) {
      error = err.message || 'Failed to load backup slots';
    } finally {
      loading = false;
    }
  }

  async function run(action, done) {
    working = true;
    error = '';
    success = '';
    try {
      await action();
      success = done;
      await load();
    } catch (err) {
      error = err.message || 'Request failed';
    } finally {
      working = false;
    }
  }

  function release(slot) {
    if (!confirm(`Delete the backup kept in ${slot.name}? This cannot be undone.`)) return;
    run(() => clientReleaseSlotBackup(slot.service_id), 'Backup released and deleted.');
  }

  function restore(slot) {
    const targetId = Number(targets[slot.service_id]);
    if (!targetId) return;
    const target = (slot.restore_targets || []).find((t) => t.service_id === targetId);
    if (
      !confirm(
        `Restore this backup onto ${target?.name || 'the chosen VM'}? Everything on that VM is replaced and cannot be recovered. ` +
          'Its address, size and login details stay as they are.'
      )
    )
      return;
    run(
      () => clientRestoreSlotBackup(slot.service_id, { target_service_id: targetId, confirm_data_loss: true }),
      'Restore started. The VM will restart when it finishes.'
    );
  }

  onMount(load);
</script>

<section aria-labelledby="slots-title">
  <div class="page-head">
    <div>
      <h1 id="slots-title">Backup slots</h1>
      <p class="sub">
        A slot keeps one backup of your VMs, even after the VM is cancelled. Keep a backup from a VM's Backups tab, then restore it onto any of your VMs.
      </p>
    </div>
  </div>

  {#if error}<Alert type="danger">{error}</Alert>{/if}
  {#if success}<Alert type="success">{success}</Alert>{/if}

  {#if loading}
    <div class="state"><Spinner /><span>Loading...</span></div>
  {:else if slots.length === 0}
    <div class="empty">
      <h2>No backup slots yet</h2>
      <p>Order a backup slot to keep a backup after you cancel a server.</p>
    </div>
  {:else}
    {#each slots as slot (slot.service_id)}
      <article class="panel">
        <div class="head">
          <div>
            <h2>{slot.name}</h2>
            <p>Holds one backup up to {slot.size_gb} GB</p>
          </div>
          <span class="badge" class:ok={slot.status === 'active'}>{STATUS_LABELS[slot.status] || slot.status}</span>
        </div>

        {#if slot.status === 'suspended'}
          <p class="hint">This slot is suspended. Your backup is kept, but it cannot be restored until the slot is active again.</p>
        {/if}

        {#if slot.retained}
          <dl class="kept">
            <div><dt>Label</dt><dd>{slot.retained.name || '-'}</dd></div>
            <div><dt>From</dt><dd>{slot.retained.source_service_name || '-'}</dd></div>
            <div><dt>Taken</dt><dd>{formatTime(slot.retained.backup_created_at)}</dd></div>
            <div><dt>Size</dt><dd>{formatBytes(slot.retained.size_bytes)}</dd></div>
          </dl>

          {#if slot.status === 'active' && (slot.restore_targets || []).length}
            <div class="restore">
              <select bind:value={targets[slot.service_id]} aria-label="Restore onto" disabled={working}>
                <option value="" disabled selected>Restore onto...</option>
                {#each slot.restore_targets as t}
                  <option value={t.service_id}>{t.name}{t.is_source ? ' (original VM)' : ''}</option>
                {/each}
              </select>
              <Button variant="secondary" disabled={working || !targets[slot.service_id]} on:click={() => restore(slot)}>
                Restore
              </Button>
            </div>
            <p class="warn">Restoring replaces everything on the chosen VM. Its address, size and login details stay as they are.</p>
          {:else if slot.status === 'active'}
            <p class="hint">You have no active VM to restore onto.</p>
          {/if}

          <div class="release">
            <Button variant="danger" size="small" disabled={working} on:click={() => release(slot)}>Release and delete backup</Button>
          </div>
        {:else}
          <p class="hint">This slot is empty. Open one of your VMs, go to Backups and choose "Keep in slot".</p>
        {/if}
      </article>
    {/each}
  {/if}
</section>

<style>
  .page-head { display: flex; align-items: flex-end; justify-content: space-between; gap: 16px; margin-bottom: 20px; }
  .page-head h1 { margin: 0; font-size: 26px; font-weight: 750; letter-spacing: -0.02em; }
  .sub { margin: 4px 0 0; font-size: 14px; color: var(--text-tertiary); }
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
  .head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; margin-bottom: 10px; }
  .head p { margin: 0; color: var(--text-secondary); font-size: 13px; }
  .badge { display: inline-block; padding: 2px 9px; border-radius: var(--radius-pill); border: 1px solid var(--border-color); font-size: 12px; font-weight: 600; color: var(--warning-color, #b7791f); }
  .badge.ok { color: var(--success-color, #2e7d32); border-color: var(--success-color, #2e7d32); }
  .hint { margin: 10px 0 0; font-size: 13px; color: var(--text-tertiary); line-height: 1.5; }
  .warn { margin: 8px 0 0; font-size: 13px; color: var(--danger-color); }
  .kept { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 10px; margin: 8px 0 14px; }
  .kept dt { font-size: 12px; color: var(--text-tertiary); }
  .kept dd { margin: 2px 0 0; font-size: 14px; font-weight: 600; }
  .restore { display: grid; grid-template-columns: 1fr auto; gap: 8px; }
  .release { margin-top: 14px; }
  select { padding: 9px 10px; border: 1px solid var(--border-color); border-radius: 8px; background: var(--bg-primary); color: var(--text-primary); min-width: 0; }
  .empty { text-align: center; padding: 56px 24px; background: var(--portal-card-bg); border: 1px dashed var(--border-color); border-radius: var(--radius-lg); color: var(--text-tertiary); }
  .empty h2 { margin: 0 0 6px; font-size: 18px; font-weight: 700; color: var(--text-primary); }
  .empty p { margin: 0; font-size: 14px; }
</style>
