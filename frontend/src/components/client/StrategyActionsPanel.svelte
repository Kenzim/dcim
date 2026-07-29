<script>
  import { onMount } from 'svelte';
  import { Alert, Button } from '../ui/index.js';
  import { listServiceStrategyActions, runServiceStrategyAction } from '../../lib/api.js';

  /** VM service id. */
  export let serviceId;

  let actions = [];
  let password = '';
  let busy = false;
  let message = '';
  let error = '';

  $: hasPasswordAction = actions.some((a) => a.name === 'change_password');

  onMount(async () => {
    try {
      const res = await listServiceStrategyActions(serviceId, { client: true });
      actions = res.actions || [];
    } catch (_) {
      // No actions available for this service's template — hide the panel.
      actions = [];
    }
  });

  async function run(action) {
    if (busy) return;
    busy = true;
    message = '';
    error = '';
    try {
      const params = {};
      if (action.name === 'change_password') {
        if (!password) throw new Error('Enter a new password first');
        params.password = password;
      }
      await runServiceStrategyAction(serviceId, action.name, params, { client: true });
      message = `${action.label} succeeded`;
      password = '';
    } catch (e) {
      error = e.message || String(e);
    } finally {
      busy = false;
    }
  }
</script>

{#if actions.length}
  <section class="panel">
    <header class="panel-head">
      <h3>Guest actions</h3>
      <p class="muted">Actions available for this service's operating system.</p>
    </header>

    {#if error}<Alert type="error">{error}</Alert>{/if}

    <div class="actions">
      {#if hasPasswordAction}
        <input
          type="password"
          placeholder="New password"
          bind:value={password}
          disabled={busy}
          autocomplete="new-password"
          aria-label="New guest password"
        />
      {/if}
      {#each actions as action}
        <Button variant="secondary" disabled={busy} on:click={() => run(action)} title={action.description || ''}>
          {action.label}
        </Button>
      {/each}
    </div>
    {#if message}<p class="ok">{message}</p>{/if}
  </section>
{/if}

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
  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  input {
    flex: 1 1 180px;
    min-width: 160px;
    padding: 10px 12px;
    border: 2px solid var(--border-color);
    border-radius: 8px;
    font-size: 14px;
    font-family: inherit;
    background: var(--bg-primary);
    color: var(--text-primary);
  }
  input:focus {
    outline: none;
    border-color: var(--accent-color);
    box-shadow: var(--focus-ring-accent);
  }
  .ok {
    margin: 10px 0 0;
    font-size: 13px;
    color: var(--success-color);
  }
</style>
