<script>
  /**
   * Config-driven server controls panel.
   * Renders capability cards from server.effective_capabilities.
   */
  import CapabilityCard from './ui/CapabilityCard.svelte';

  export let server;
  export let effectiveCapabilities = [];

  $: caps = effectiveCapabilities && effectiveCapabilities.length > 0
    ? effectiveCapabilities
    : (server && server.effective_capabilities) || [];
</script>

{#if caps.length > 0}
  <div class="server-controls-panel">
    {#each caps as capability}
      <CapabilityCard
        serverId={server?.id}
        capability={capability}
        serverName={server?.name || ''}
      />
    {/each}
  </div>
{/if}

<style>
  .server-controls-panel {
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }
</style>
