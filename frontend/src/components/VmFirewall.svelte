<script>
  import PageHeader from './PageHeader.svelte';
  import { Tabs } from './ui/index.js';
  import LocationsTab from './firewall/admin/LocationsTab.svelte';
  import BaselinesTab from './firewall/admin/BaselinesTab.svelte';
  import StatusTab from './firewall/admin/StatusTab.svelte';
  import FirewallGroupsPanel from './firewall/FirewallGroupsPanel.svelte';
  import FirewallIpsetsPanel from './firewall/FirewallIpsetsPanel.svelte';

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
  .stack { display: flex; flex-direction: column; gap: 16px; }
  @media (max-width: 768px) { .page { padding: 16px; } }
</style>
