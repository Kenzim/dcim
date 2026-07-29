<script>
  import { createEventDispatcher } from 'svelte';
  import { Alert, Button } from '../ui/index.js';
  import { putVmSshKeys } from '../../lib/api.js';

  /** VM service object (uses `id` and `ssh_public_keys_text`). */
  export let service;

  const dispatch = createEventDispatcher();

  let text = service?.ssh_public_keys_text || '';
  let busy = false;
  let message = '';
  let error = '';

  async function save() {
    if (busy) return;
    busy = true;
    message = '';
    error = '';
    try {
      const res = await putVmSshKeys(service.id, text, { client: true });
      message = res.applied
        ? 'SSH keys saved and applied.'
        : 'SSH keys saved (apply pending if guest agent is down).';
      dispatch('saved');
    } catch (e) {
      error = e.message || String(e);
    } finally {
      busy = false;
    }
  }
</script>

<section class="panel">
  <header class="panel-head">
    <h3>SSH public keys</h3>
    <p class="muted">One OpenSSH public key per line. Applied to the default guest user via the QEMU guest agent.</p>
  </header>

  {#if error}<Alert type="error">{error}</Alert>{/if}

  <textarea
    rows="4"
    placeholder="ssh-ed25519 AAAA… you@host"
    bind:value={text}
    disabled={busy}
    aria-label="SSH public keys"
  ></textarea>
  <div class="actions">
    <Button disabled={busy} on:click={save}>Save SSH keys</Button>
    {#if message}<span class="ok">{message}</span>{/if}
  </div>
</section>

<style>
  .panel-head h3 {
    margin: 0 0 4px;
    font-size: 16px;
    font-weight: 700;
  }
  .panel-head p {
    margin: 0 0 14px;
  }
  .muted {
    color: var(--text-tertiary);
    font-size: 13px;
  }
  textarea {
    width: 100%;
    padding: 10px 12px;
    border: 2px solid var(--border-color);
    border-radius: 8px;
    font-size: 13px;
    font-family: var(--font-mono);
    background: var(--bg-primary);
    color: var(--text-primary);
    resize: vertical;
  }
  textarea:focus {
    outline: none;
    border-color: var(--accent-color);
    box-shadow: var(--focus-ring-accent);
  }
  .actions {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-top: 10px;
  }
  .ok {
    font-size: 13px;
    color: var(--success-color);
  }
</style>
