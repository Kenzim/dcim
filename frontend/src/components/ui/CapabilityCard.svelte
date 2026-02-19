<script>
  /**
   * Dispatches to the appropriate capability renderer based on ui_pattern.
   */
  import StateAndActionsCard from './StateAndActionsCard.svelte';

  export let serverId;
  export let capability;
  export let serverName = '';
</script>

{#if capability.ui_pattern === 'state_and_actions'}
  <StateAndActionsCard {serverId} {capability} {serverName} />
{:else if capability.ui_pattern === 'ordered_list' || capability.ui_pattern === 'list_and_actions'}
  <!-- Placeholder for future: OrderedListCard, ListAndActionsCard -->
  <div class="card">
    <div class="card-header">
      <h3>{capability.display_name}</h3>
    </div>
    <div class="card-body">
      <p class="text-muted">UI pattern "{capability.ui_pattern}" not yet implemented.</p>
    </div>
  </div>
{:else}
  <div class="card">
    <div class="card-header">
      <h3>{capability.display_name}</h3>
    </div>
    <div class="card-body">
      <p class="text-muted">Unknown ui_pattern: {capability.ui_pattern || 'none'}</p>
    </div>
  </div>
{/if}

<style>
  .text-muted {
    color: #6b7280;
    font-size: 0.9rem;
  }
</style>
