<script>
  import { onMount } from 'svelte';
  import { Alert, Button, Spinner, Tabs } from '../ui/index.js';
  import { getClientService } from '../../lib/api.js';
  import ServiceStatusBadge from './ServiceStatusBadge.svelte';
  import PowerControls from './PowerControls.svelte';
  import VmServiceView from './VmServiceView.svelte';
  import BareMetalServiceView from './BareMetalServiceView.svelte';
  import ProxyServiceView from './ProxyServiceView.svelte';

  /** @type {number} */
  export let serviceId;

  let detail = null;
  let loading = true;
  let error = '';
  let activeTab = 'overview';

  const TYPE_LABELS = {
    vm: 'Virtual machine',
    bare_metal: 'Bare metal',
    http_proxy: 'HTTP proxy',
  };

  async function load() {
    try {
      detail = await getClientService(serviceId);
      error = '';
    } catch (e) {
      error = e.message || String(e);
    } finally {
      loading = false;
    }
  }

  onMount(load);

  $: tabs = buildTabs(detail);
  $: if (tabs.length && !tabs.some((t) => t.id === activeTab)) {
    activeTab = tabs[0].id;
  }

  function buildTabs(d) {
    if (!d) return [];
    if (d.service_type === 'vm') {
      const t = [{ id: 'overview', label: 'Overview' }];
      if (d.console_available) t.push({ id: 'console', label: 'Console' });
      if (d.backups_available) t.push({ id: 'backups', label: 'Backups' });
      if (d.permissions?.['vm.reinstall']) t.push({ id: 'reinstall', label: 'Reinstall' });
      const p = d.permissions || {};
      if (d.accepts_ssh_key || p['vm.manage_ssh_keys'] || p['vm.change_password'] || p['vm.reset_network']) {
        t.push({ id: 'settings', label: 'Settings' });
      }
      return t;
    }
    if (d.service_type === 'bare_metal') {
      const t = [{ id: 'overview', label: 'Overview' }];
      if (d.ipmi_available || d.kvm_console_available) t.push({ id: 'console', label: 'Console' });
      return t;
    }
    return [];
  }
</script>

<div class="detail">
  <a href="/client/services" class="back-link">
    <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" width="14" height="14" aria-hidden="true">
      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 19l-7-7 7-7" />
    </svg>
    All services
  </a>

  {#if loading}
    <div class="loading"><Spinner /> <span>Loading service…</span></div>
  {:else if error}
    <Alert type="error">{error}</Alert>
    <Button variant="secondary" on:click={load}>Retry</Button>
  {:else if detail}
    <header class="head-card">
      <div class="head-main">
        <div class="head-title">
          <h1>{detail.name}</h1>
          <span class="type-label">{TYPE_LABELS[detail.service_type] || 'Service'}</span>
        </div>
        <div class="head-badges">
          <ServiceStatusBadge status={detail.status} powerState={detail.power_state} />
        </div>
        {#if detail.primary_ip}
          <div class="head-ip">
            <span class="ip-label">Primary IP</span>
            <code>{detail.primary_ip}</code>
          </div>
        {/if}
      </div>
      <PowerControls service={detail} showReset on:changed={load} />
    </header>

    {#if tabs.length}
      <Tabs {tabs} bind:active={activeTab} />
    {/if}

    <div class="tab-body">
      {#if detail.service_type === 'vm'}
        <VmServiceView service={detail} {activeTab} on:refresh={load} />
      {:else if detail.service_type === 'bare_metal'}
        <BareMetalServiceView service={detail} {activeTab} on:refresh={load} />
      {:else if detail.service_type === 'http_proxy'}
        <ProxyServiceView service={detail} />
      {:else}
        <p class="muted">This service type has no portal controls.</p>
      {/if}
    </div>
  {/if}
</div>

<style>
  .detail {
    display: flex;
    flex-direction: column;
    gap: 18px;
  }
  .back-link {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    align-self: flex-start;
    font-size: 13px;
    font-weight: 650;
    color: var(--text-tertiary);
    text-decoration: none;
  }
  .back-link:hover {
    color: var(--portal-accent);
  }
  .loading {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 48px 0;
    justify-content: center;
    color: var(--text-secondary);
  }
  .head-card {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 16px;
    padding: 22px 24px;
    background: var(--portal-card-bg);
    border: 1px solid var(--portal-card-border);
    border-radius: var(--radius-lg);
    box-shadow: var(--portal-card-shadow);
  }
  .head-main {
    display: flex;
    flex-direction: column;
    gap: 8px;
    min-width: 0;
  }
  .head-title {
    display: flex;
    align-items: baseline;
    gap: 12px;
    flex-wrap: wrap;
  }
  .head-title h1 {
    margin: 0;
    font-size: 22px;
    font-weight: 750;
    letter-spacing: -0.02em;
    word-break: break-word;
  }
  .type-label {
    font-size: 13px;
    color: var(--text-tertiary);
  }
  .head-badges {
    display: flex;
    gap: 8px;
  }
  .head-ip {
    display: flex;
    align-items: center;
    gap: 10px;
    font-size: 14px;
  }
  .ip-label {
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    color: var(--text-tertiary);
  }
  .head-ip code {
    font-family: var(--font-mono);
    font-size: 14px;
    font-weight: 600;
  }
  .tab-body {
    background: var(--portal-card-bg);
    border: 1px solid var(--portal-card-border);
    border-radius: var(--radius-lg);
    box-shadow: var(--portal-card-shadow);
    padding: 22px 24px;
  }
  .muted {
    color: var(--text-tertiary);
    font-size: 14px;
  }
  @media (max-width: 640px) {
    .head-card,
    .tab-body {
      padding: 16px;
    }
  }
</style>
