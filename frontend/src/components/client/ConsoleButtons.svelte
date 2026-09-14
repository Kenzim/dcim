<script>
  import { onMount } from 'svelte';
  import { Button } from '../ui/index.js';
  import { getClientVmConsoleTypes } from '../../lib/api.js';

  /** VM service object (uses `id`). */
  export let service;

  // undefined = not checked yet (show the single generic button)
  let consoleTypes;

  onMount(async () => {
    try {
      consoleTypes = await getClientVmConsoleTypes(service.id);
    } catch (_) {
      consoleTypes = undefined;
    }
  });

  function openConsole(type) {
    // Opened synchronously (before any await) so browsers don't treat this
    // as a blocked pop-up: the ticket-mint + /vnc redirect happens entirely
    // server-side (see GET .../vm/vnc-popup), authenticated by the caller's
    // own session cookie. A real top-level window gives the console its own
    // clipboard/focus context, unlike an in-page modal.
    const query = type ? `?type=${encodeURIComponent(type)}` : '';
    window.open(
      `/api/client/services/${service.id}/vm/vnc-popup${query}`,
      `rackflow_console_${service.id}`,
      'width=1400,height=960,resizable=yes,scrollbars=yes'
    );
  }
</script>

<div class="console-buttons">
  {#if consoleTypes?.vnc && consoleTypes?.serial}
    <Button on:click={() => openConsole('vnc')}>
      <svg slot="icon" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" width="15" height="15">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" />
      </svg>
      Open noVNC
    </Button>
    <Button variant="secondary" on:click={() => openConsole('serial')}>
      <svg slot="icon" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" width="15" height="15">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
      </svg>
      Serial console
    </Button>
  {:else}
    <Button on:click={() => openConsole()}>
      <svg slot="icon" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" width="15" height="15">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" />
      </svg>
      Open console
    </Button>
  {/if}
</div>

<style>
  .console-buttons {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
  }
</style>
