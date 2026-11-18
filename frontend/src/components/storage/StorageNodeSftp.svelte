<script>
  import { createEventDispatcher } from 'svelte';
  import Button from '../ui/Button.svelte';

  // A storage node from /api/admin/storage/nodes.
  export let node;
  export let disabled = false;

  const dispatch = createEventDispatcher();
  let host = node.sftp_host || '';
  let port = node.sftp_port || 2222;

  $: state = !node.sftp_enabled
    ? 'Off'
    : node.sftp_error
      ? 'Problem'
      : node.sftp_listening
        ? 'Listening'
        : 'Waiting for the runner';

  function save(enabled) {
    dispatch('save', { sftp_host: host.trim(), sftp_port: Number(port), sftp_enabled: enabled });
  }
</script>

<div class="sftp">
  <span class="hint">Public SFTP endpoint: {state}</span>
  <label>Public host <input bind:value={host} placeholder="sftp1.example.net" {disabled} /></label>
  <label>Port <input type="number" min="1" max="65535" bind:value={port} {disabled} /></label>
  {#if node.sftp_host_key_fingerprint}<span class="hint mono">Host key {node.sftp_host_key_fingerprint}</span>{/if}
  {#if node.sftp_error}<p class="err">{node.sftp_error}</p>{/if}
  <div class="actions">
    <Button size="small" disabled={disabled || !host.trim()} on:click={() => save(node.sftp_enabled)}>Save</Button>
    <Button size="small" variant="secondary" disabled={disabled || (!node.sftp_enabled && !host.trim())} on:click={() => save(!node.sftp_enabled)}>
      {node.sftp_enabled ? 'Turn SFTP off' : 'Turn SFTP on'}
    </Button>
  </div>
</div>

<style>
  .sftp { display: flex; flex-direction: column; gap: 6px; border-top: 1px solid var(--border-color); padding-top: 8px; }
  .hint { font-size: 0.85rem; color: var(--text-secondary); }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; word-break: break-all; }
  .err { margin: 0; color: var(--danger-color); font-size: 0.85rem; }
  label { display: flex; flex-direction: column; gap: 4px; font-size: 0.85rem; font-weight: 600; }
  input { padding: 6px 8px; border: 1px solid var(--border-color); border-radius: 6px; background: var(--bg-secondary); color: var(--text-primary); }
  .actions { display: flex; gap: 6px; }
</style>
