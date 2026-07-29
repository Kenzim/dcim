<script>
  import { onMount } from 'svelte';
  import { Alert } from '../ui/index.js';
  import { listMyServices } from '../../lib/api.js';
  import ServiceCard from './ServiceCard.svelte';

  let services = [];
  let loading = true;
  let error = '';
  let filter = 'all';

  const FILTERS = [
    { id: 'all', label: 'All' },
    { id: 'vm', label: 'Virtual machines' },
    { id: 'bare_metal', label: 'Bare metal' },
    { id: 'http_proxy', label: 'HTTP proxies' },
  ];

  $: filtered = filter === 'all' ? services : services.filter((s) => s.service_type === filter);
  $: counts = services.reduce((acc, s) => {
    acc[s.service_type] = (acc[s.service_type] || 0) + 1;
    return acc;
  }, {});

  onMount(async () => {
    try {
      services = await listMyServices();
    } catch (e) {
      error = e.message || String(e);
    } finally {
      loading = false;
    }
  });
</script>

<div class="page-head">
  <div>
    <h1>Services</h1>
    <p class="sub">{services.length} service{services.length === 1 ? '' : 's'} on your account</p>
  </div>
</div>

{#if loading}
  <div class="grid">
    {#each [1, 2, 3] as _}
      <div class="skeleton" aria-hidden="true"></div>
    {/each}
  </div>
{:else if error}
  <Alert type="error">{error}</Alert>
{:else if services.length === 0}
  <div class="empty">
    <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" width="44" height="44" aria-hidden="true">
      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M5 12h14M5 12a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v4a2 2 0 01-2 2M5 12a2 2 0 00-2 2v4a2 2 0 002 2h14a2 2 0 002-2v-4a2 2 0 00-2-2m-2-4h.01M17 16h.01" />
    </svg>
    <h2>No services yet</h2>
    <p>When a service is provisioned for your account it will show up here.</p>
  </div>
{:else}
  <div class="filters" role="group" aria-label="Filter by type">
    {#each FILTERS as f}
      <button
        type="button"
        class="chip"
        class:chip-active={filter === f.id}
        on:click={() => (filter = f.id)}
      >
        {f.label}
        {#if f.id !== 'all' && counts[f.id]}<span class="chip-count">{counts[f.id]}</span>{/if}
      </button>
    {/each}
  </div>

  {#if filtered.length === 0}
    <p class="no-match">No services of this type.</p>
  {:else}
    <div class="grid">
      {#each filtered as service (service.id)}
        <ServiceCard {service} />
      {/each}
    </div>
  {/if}
{/if}

<style>
  .page-head {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 16px;
    margin-bottom: 20px;
  }
  .page-head h1 {
    margin: 0;
    font-size: 26px;
    font-weight: 750;
    letter-spacing: -0.02em;
  }
  .sub {
    margin: 4px 0 0;
    font-size: 14px;
    color: var(--text-tertiary);
  }
  .filters {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 18px;
  }
  .chip {
    appearance: none;
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 7px 14px;
    border: 1px solid var(--border-color);
    border-radius: var(--radius-pill);
    background: var(--bg-primary);
    color: var(--text-secondary);
    font-family: inherit;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.15s ease;
  }
  .chip:hover {
    border-color: var(--portal-accent);
    color: var(--portal-accent);
  }
  .chip-active {
    background: var(--portal-accent);
    border-color: var(--portal-accent);
    color: var(--portal-accent-contrast);
  }
  .chip-count {
    font-size: 11px;
    font-weight: 750;
    padding: 1px 7px;
    border-radius: var(--radius-pill);
    background: rgba(127, 127, 127, 0.18);
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(330px, 1fr));
    gap: 16px;
  }
  .skeleton {
    height: 180px;
    border-radius: var(--radius-lg);
    background: linear-gradient(100deg, var(--bg-tertiary) 40%, var(--bg-secondary) 50%, var(--bg-tertiary) 60%);
    background-size: 200% 100%;
    animation: shimmer 1.4s infinite;
  }
  @keyframes shimmer {
    to {
      background-position: -200% 0;
    }
  }
  .empty {
    text-align: center;
    padding: 64px 24px;
    background: var(--portal-card-bg);
    border: 1px dashed var(--border-color);
    border-radius: var(--radius-lg);
    color: var(--text-tertiary);
  }
  .empty h2 {
    margin: 14px 0 6px;
    font-size: 18px;
    font-weight: 700;
    color: var(--text-primary);
  }
  .empty p {
    margin: 0;
    font-size: 14px;
  }
  .no-match {
    color: var(--text-tertiary);
    font-size: 14px;
  }
  @media (max-width: 640px) {
    .grid {
      grid-template-columns: 1fr;
    }
  }
</style>
