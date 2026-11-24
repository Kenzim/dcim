<script>
  import PageHeader from './PageHeader.svelte';
  import { Tabs } from './ui/index.js';
  import LocationsTab from './firewall/admin/LocationsTab.svelte';
  import BaselinesTab from './firewall/admin/BaselinesTab.svelte';
  import StatusTab from './firewall/admin/StatusTab.svelte';
  import FirewallGroupsPanel from './firewall/FirewallGroupsPanel.svelte';
  import FirewallIpsetsPanel from './firewall/FirewallIpsetsPanel.svelte';

  import { onMount } from 'svelte';
  import { getWorkerStatus } from '../lib/vmFirewallApi.js';

  let worker = null;
  const when = (iso) => (iso ? new Date(iso).toLocaleString() : 'never');
  onMount(() => getWorkerStatus().then((status) => (worker = status)).catch(() => (worker = null)));

  const tabs = [
    { id: 'locations', label: 'Locations' },
    { id: 'baselines', label: 'Baselines' },
    { id: 'objects', label: 'Groups and IP sets' },
    { id: 'status', label: 'Status' },
  ];
  let active = 'locations';
</script>

<PageHeader title="VM Firewall" />
<div class="page">
  <p class="worker">
    {#if worker === null}
      Background worker: status unavailable.
    {:else if !worker.last_tick_at}
      Background worker: has not reported yet. Nothing is applied to hypervisors until it runs.
    {:else}
      Background worker: last ran {when(worker.last_tick_at)}{worker.owner ? ` (${worker.owner})` : ''}{worker.last_sweep_at ? `, last drift sweep ${when(worker.last_sweep_at)}` : ''}{worker.last_readiness_at ? `, last readiness check ${when(worker.last_readiness_at)}` : ''}.
    {/if}
  </p>

  <Tabs {tabs} bind:active />

  {#if active === 'locations'}
    <LocationsTab />
  {:else if active === 'baselines'}
    <BaselinesTab />
  {:else if active === 'objects'}
    <div class="stack">
      <FirewallGroupsPanel audience="staff" />
      <FirewallIpsetsPanel audience="staff" />
    </div>
  {:else}
    <StatusTab />
  {/if}
</div>

<style>
  .page { padding: 24px; display: flex; flex-direction: column; gap: 16px; }
  .worker { margin: 0; font-size: 12px; color: var(--text-tertiary); }
  .stack { display: flex; flex-direction: column; gap: 16px; }
  @media (max-width: 768px) { .page { padding: 16px; } }
</style>
