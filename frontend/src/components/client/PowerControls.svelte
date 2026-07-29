<script>
  import { createEventDispatcher } from 'svelte';
  import { Button } from '../ui/index.js';
  import { clientServicePower } from '../../lib/api.js';

  /**
   * Service object. Uses `power_available`, `power_state`, `status`,
   * `server_enabled`, and `name`. Works with both the list payload
   * (power_available included) and the detail payload.
   */
  export let service;
  /** Small buttons for card footers. */
  export let compact = false;
  /** Also show the Reset button (detail page). */
  export let showReset = false;

  const dispatch = createEventDispatcher();

  let busy = false;
  let error = '';

  $: powerState = service?.power_state || 'unknown';
  $: suspended = ['suspended', 'terminated'].includes(String(service?.status || '').toLowerCase());
  // Only "off" is allowed for suspended/terminated services or
  // administratively disabled servers (mirrors the backend guard).
  $: canStart = !suspended && service?.server_enabled !== false;
  $: visible = !!service?.power_available;

  async function run(action) {
    if (busy) return;
    if (action === 'off' && !confirm(`Power off ${service.name}? The guest OS is not shut down cleanly.`)) return;
    if (action === 'reboot' && !confirm(`Reboot ${service.name}?`)) return;
    if (action === 'reset' && !confirm(`Force-reset ${service.name}? Unsaved data in the guest may be lost.`)) return;
    busy = true;
    error = '';
    try {
      await clientServicePower(service.id, action);
      dispatch('changed');
    } catch (e) {
      error = e.message || String(e);
    } finally {
      busy = false;
    }
  }
</script>

{#if visible}
  <div class="power" class:power-compact={compact}>
    <div class="power-buttons">
      {#if powerState !== 'on'}
        {#if canStart}
          <Button size={compact ? 'small' : 'default'} disabled={busy} on:click={() => run('on')}>
            <svg slot="icon" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" width="14" height="14">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 3v9m5.25-6.75a6 6 0 11-10.5 0" />
            </svg>
            Power on
          </Button>
        {/if}
      {:else}
        <Button variant="secondary" size={compact ? 'small' : 'default'} disabled={busy} on:click={() => run('reboot')}>
          Reboot
        </Button>
      {/if}
      {#if powerState === 'on' || suspended}
        <Button variant="danger" size={compact ? 'small' : 'default'} disabled={busy} on:click={() => run('off')}>
          Power off
        </Button>
      {/if}
      {#if showReset && canStart && powerState === 'on'}
        <Button variant="secondary" size={compact ? 'small' : 'default'} disabled={busy} on:click={() => run('reset')}>
          Reset
        </Button>
      {/if}
    </div>
    {#if error}<p class="power-error">{error}</p>{/if}
  </div>
{/if}

<style>
  .power {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .power-buttons {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  .power-error {
    margin: 0;
    font-size: 12px;
    color: var(--danger-text);
  }
  @media (max-width: 640px) {
    .power-buttons :global(.btn) {
      flex: 1 1 auto;
    }
  }
</style>
