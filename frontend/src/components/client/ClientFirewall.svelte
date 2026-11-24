<script>
  import { Tabs } from '../ui/index.js';
  import FirewallGroupsPanel from '../firewall/FirewallGroupsPanel.svelte';
  import FirewallIpsetsPanel from '../firewall/FirewallIpsetsPanel.svelte';

  const tabs = [
    { id: 'groups', label: 'Security groups' },
    { id: 'ipsets', label: 'IP sets' },
  ];
  let active = 'groups';
</script>

<section aria-labelledby="firewall-title">
  <div class="page-head">
    <h1 id="firewall-title">Firewall</h1>
    <p class="sub">
      Security groups and IP sets you can reuse on any of your virtual machines. Each machine's own rules are on its
      <a href="/client/services">service page</a>, under Firewall.
    </p>
  </div>

  <Tabs {tabs} bind:active />

  <div class="body">
    {#if active === 'groups'}
      <FirewallGroupsPanel audience="customer" />
    {:else}
      <FirewallIpsetsPanel audience="customer" />
    {/if}
  </div>
</section>

<style>
  .page-head { margin-bottom: 20px; }
  .page-head h1 { margin: 0; font-size: 26px; font-weight: 750; letter-spacing: -0.02em; }
  .sub { margin: 4px 0 0; font-size: 14px; color: var(--text-tertiary); }
  .sub a { color: var(--portal-accent); }
  .body { margin-top: 16px; }
</style>
