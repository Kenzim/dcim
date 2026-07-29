<script>
  import { createEventDispatcher, onMount } from 'svelte';
  import { Alert, Button, Modal } from '../ui/index.js';
  import { getVmSshKeys, reinstallVmGuest } from '../../lib/api.js';

  /** VM service object (uses `id`, `name`, `vm_template_id`, ssh flags). */
  export let service;
  export let onClose;

  const dispatch = createEventDispatcher();

  let templateId = service?.vm_template_id != null ? String(service.vm_template_id) : '';
  let templates = [];
  let sshKeys = service?.ssh_public_keys_text || '';
  let busy = false;
  let error = '';

  onMount(async () => {
    try {
      const info = await getVmSshKeys(service.id, { client: true });
      templates = info.reinstall_templates || [];
      sshKeys = info.ssh_public_keys_text || sshKeys;
    } catch (_) {
      // Template choices are optional; the reinstall works without them.
    }
  });

  $: acceptsKeys = (() => {
    const id = templateId ? Number(templateId) : null;
    if (id && templates.length) {
      const t = templates.find((x) => Number(x.id) === id);
      if (t) return !!t.accepts_ssh_key;
    }
    return !!(service.accepts_ssh_key || service.needs_ssh_key_prompt);
  })();

  async function confirmReinstall() {
    if (busy) return;
    if (!confirm('Reinstall this VM at the same VMID? Disks are replaced; backups stay available to restore.')) return;
    busy = true;
    error = '';
    const data = {};
    if (templateId) data.vm_template_id = Number(templateId);
    if (acceptsKeys) data.ssh_public_keys = sshKeys || '';
    try {
      await reinstallVmGuest(service.id, data, { client: true });
      dispatch('done');
      onClose?.();
    } catch (e) {
      error = e.message || String(e);
    } finally {
      busy = false;
    }
  }
</script>

<Modal title={`Reinstall ${service.name}`} {onClose}>
  <p class="muted">
    Rebuilds the guest from its template. Optionally switch template or pre-seed SSH keys (leave blank to skip).
  </p>

  {#if error}<Alert type="error">{error}</Alert>{/if}

  {#if templates.length}
    <div class="form-group">
      <label for="reinstall-template">Template</label>
      <select id="reinstall-template" bind:value={templateId} disabled={busy}>
        <option value="">Keep current template</option>
        {#each templates as t}
          <option value={String(t.id)}>{t.name}</option>
        {/each}
      </select>
    </div>
  {/if}

  {#if acceptsKeys}
    <div class="form-group">
      <label for="reinstall-keys">SSH public keys</label>
      <textarea
        id="reinstall-keys"
        rows="4"
        placeholder="One key per line (optional)"
        bind:value={sshKeys}
        disabled={busy}
      ></textarea>
    </div>
  {/if}

  <div slot="footer">
    <Button variant="secondary" disabled={busy} on:click={onClose}>Cancel</Button>
    <Button variant="danger" disabled={busy} on:click={confirmReinstall}>
      {busy ? 'Queuing…' : 'Reinstall'}
    </Button>
  </div>
</Modal>

<style>
  .muted {
    margin: 0 0 16px;
    color: var(--text-tertiary);
    font-size: 13px;
  }
</style>
