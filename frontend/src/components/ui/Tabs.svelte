<script>
  import { createEventDispatcher } from 'svelte';

  /** @type {{ id: string, label: string }[]} */
  export let tabs = [];
  /** @type {string} id of the active tab */
  export let active = '';

  const dispatch = createEventDispatcher();

  function select(id) {
    if (id === active) return;
    active = id;
    dispatch('change', { id });
  }
</script>

<div class="tabs" role="tablist" aria-label="Sections">
  {#each tabs as tab}
    <button
      type="button"
      class="tab"
      class:tab-active={tab.id === active}
      role="tab"
      aria-selected={tab.id === active}
      on:click={() => select(tab.id)}
    >
      {tab.label}
    </button>
  {/each}
</div>

<style>
  .tabs {
    display: flex;
    gap: 4px;
    border-bottom: 1px solid var(--border-color);
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
    scrollbar-width: none;
  }
  .tabs::-webkit-scrollbar {
    display: none;
  }
  .tab {
    appearance: none;
    background: none;
    border: none;
    border-bottom: 2px solid transparent;
    margin-bottom: -1px;
    padding: 10px 14px;
    font-family: inherit;
    font-size: 14px;
    font-weight: 600;
    color: var(--text-tertiary);
    cursor: pointer;
    white-space: nowrap;
    transition: color 0.15s ease, border-color 0.15s ease;
  }
  .tab:hover {
    color: var(--text-primary);
  }
  .tab-active {
    color: var(--portal-accent, var(--accent-color));
    border-bottom-color: var(--portal-accent, var(--accent-color));
  }
</style>
