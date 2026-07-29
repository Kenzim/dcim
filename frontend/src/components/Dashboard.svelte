<script>
  import { onMount } from 'svelte';
  import Spinner from './ui/Spinner.svelte';
  import Alert from './ui/Alert.svelte';
  import HoverTip from './ui/HoverTip.svelte';
  import AggregateTrafficPanel from './AggregateTrafficPanel.svelte';
  import {
    getLocations,
    getRacks,
    getServers,
    getSwitches,
    getServerGroups,
    getServices,
    listClients,
    listAdmins,
    listServiceInstances,
    getLocationDHCPStatus,
    getLocationTFTPStatus,
    getBillingIntegrations,
    listProxmoxClusters,
    listIpamSubnets,
    listVmIpAllocations,
    getOSTemplates,
    getPlugins,
  } from '../lib/api.js';

  let loading = true;
  let sectionErrors = {};

  let locations = [];
  let racks = [];
  let servers = [];
  let switches = [];
  let serverGroups = [];
  let services = [];
  let clients = [];
  let admins = [];
  let billingIntegrations = [];
  let proxmoxClusters = [];
  let ipamSubnets = [];
  let vmIpAllocations = [];
  let osTemplates = [];
  let plugins = [];

  // [{ id, name, health: 'healthy' | 'issues' | 'unconfigured', dhcp, tftp }]
  let locationHealth = [];
  let healthLoading = false;

  onMount(load);

  async function load() {
    loading = true;
    sectionErrors = {};

    const results = await Promise.allSettled([
      getLocations(),
      getRacks(),
      getServers(),
      getSwitches(),
      getServerGroups(),
      getServices(),
      listClients(),
      listAdmins(),
      getBillingIntegrations(),
      listProxmoxClusters(),
      listIpamSubnets(),
      listVmIpAllocations(),
      getOSTemplates(),
      getPlugins(),
    ]);

    const [
      locationsRes,
      racksRes,
      serversRes,
      switchesRes,
      serverGroupsRes,
      servicesRes,
      clientsRes,
      adminsRes,
      billingRes,
      proxmoxRes,
      ipamRes,
      vmIpRes,
      osTemplatesRes,
      pluginsRes,
    ] = results;

    locations = pick(locationsRes, 'locations');
    racks = pick(racksRes, 'racks');
    servers = pick(serversRes, 'servers');
    switches = pick(switchesRes, 'switches');
    serverGroups = pick(serverGroupsRes, 'serverGroups');
    services = pick(servicesRes, 'services');
    clients = pick(clientsRes, 'clients');
    admins = pick(adminsRes, 'admins');
    billingIntegrations = pick(billingRes, 'billing');
    proxmoxClusters = pick(proxmoxRes, 'proxmox');
    ipamSubnets = pick(ipamRes, 'ipam');
    vmIpAllocations = pick(vmIpRes, 'vmIp');
    osTemplates = pick(osTemplatesRes, 'osTemplates');
    plugins = pick(pluginsRes, 'plugins');

    loading = false;
    await loadLocationHealth();
  }

  function pick(result, key) {
    if (result.status === 'fulfilled') return result.value || [];
    sectionErrors = { ...sectionErrors, [key]: true };
    return [];
  }

  async function loadLocationHealth() {
    if (!locations.length) {
      locationHealth = [];
      return;
    }
    healthLoading = true;
    locationHealth = await Promise.all(
      locations.map(async (loc) => {
        try {
          const instances = await listServiceInstances(loc.id);
          const dhcpInstance = instances.find((i) => i.service_type === 'dhcp');
          const tftpInstance = instances.find((i) => i.service_type === 'tftp');

          let dhcp = null;
          let tftp = null;
          const checks = [];

          if (dhcpInstance) {
            dhcp = await getLocationDHCPStatus(loc.id).catch(() => ({ status: 'error', running: false }));
            checks.push(!!dhcp.running);
          }
          if (tftpInstance) {
            tftp = await getLocationTFTPStatus(loc.id).catch(() => ({ status: 'error', running: false }));
            checks.push(!!tftp.running);
          }

          let health = 'unconfigured';
          if (checks.length) {
            health = checks.every(Boolean) ? 'healthy' : 'issues';
          }

          return { id: loc.id, name: loc.name, health, dhcp, tftp };
        } catch (_) {
          return { id: loc.id, name: loc.name, health: 'issues', dhcp: null, tftp: null };
        }
      })
    );
    healthLoading = false;
  }

  function groupCount(list, field) {
    const counts = {};
    for (const item of list) {
      const key = item?.[field] || 'unknown';
      counts[key] = (counts[key] || 0) + 1;
    }
    return counts;
  }

  function statusLabel(status) {
    if (!status) return 'Not configured';
    if (status.running) return 'Running';
    if (status.status === 'error') return 'Error';
    return 'Stopped';
  }

  $: enabledServers = servers.filter((s) => s.enabled).length;
  $: disabledServers = servers.length - enabledServers;
  $: servicesByStatus = groupCount(services, 'status');
  $: issueLocations = locationHealth.filter((l) => l.health === 'issues');
  $: healthySummary =
    locations.length === 0
      ? 'No locations yet'
      : issueLocations.length === 0
        ? 'All locations healthy'
        : `${issueLocations.length} of ${locations.length} location${locations.length === 1 ? '' : 's'} need attention`;
</script>

{#if loading}
  <div class="dashboard-loading">
    <Spinner />
  </div>
{:else}
  <div class="dashboard">
    {#if Object.keys(sectionErrors).length > 0}
      <Alert type="warning">Some overview data couldn't be loaded. The rest of the page is still up to date.</Alert>
    {/if}

    <div class="dashboard-top">
      <!-- KPI strip: compact counts, hover for a short breakdown -->
      <div class="kpi-strip">
        <HoverTip>
          <a href="/admin/locations" class="kpi-tile accent-info">
            <span class="kpi-icon">
              <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17.657 16.657L13.414 20.9a1.998 1.998 0 01-2.827 0l-4.244-4.243a8 8 0 1111.314 0z" />
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 11a3 3 0 11-6 0 3 3 0 016 0z" />
              </svg>
            </span>
            <span class="kpi-text">
              <span class="kpi-value">{locations.length}</span>
              <span class="kpi-label">Locations</span>
            </span>
          </a>
          <svelte:fragment slot="content">
            <div class="tip-title">Locations</div>
            {#if locations.length}
              <div class="tip-row">Healthy: {locations.length - issueLocations.length}</div>
              <div class="tip-row">Need attention: {issueLocations.length}</div>
            {:else}
              <div class="tip-row">No locations configured yet.</div>
            {/if}
          </svelte:fragment>
        </HoverTip>

        <HoverTip>
          <a href="/admin/racks" class="kpi-tile accent-secondary">
            <span class="kpi-icon">
              <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4" />
              </svg>
            </span>
            <span class="kpi-text">
              <span class="kpi-value">{racks.length}</span>
              <span class="kpi-label">Racks</span>
            </span>
          </a>
          <svelte:fragment slot="content">
            <div class="tip-title">Racks</div>
            <div class="tip-row">Click to view all racks.</div>
          </svelte:fragment>
        </HoverTip>

        <HoverTip>
          <a href="/admin/servers" class="kpi-tile accent-accent">
            <span class="kpi-icon">
              <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 12h14M5 12a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v4a2 2 0 01-2 2M5 12a2 2 0 00-2 2v4a2 2 0 002 2h14a2 2 0 002-2v-4a2 2 0 00-2-2m-2-4h.01M17 16h.01" />
              </svg>
            </span>
            <span class="kpi-text">
              <span class="kpi-value">{servers.length}</span>
              <span class="kpi-label">Servers</span>
            </span>
          </a>
          <svelte:fragment slot="content">
            <div class="tip-title">Servers</div>
            <div class="tip-row">Enabled: {enabledServers}</div>
            <div class="tip-row">Disabled: {disabledServers}</div>
          </svelte:fragment>
        </HoverTip>

        <HoverTip>
          <a href="/admin/switches" class="kpi-tile accent-success">
            <span class="kpi-icon">
              <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 7h12m0 0l-4-4m4 4l-4 4m0 6H4m0 0l4 4m-4-4l4-4" />
              </svg>
            </span>
            <span class="kpi-text">
              <span class="kpi-value">{switches.length}</span>
              <span class="kpi-label">Switches</span>
            </span>
          </a>
          <svelte:fragment slot="content">
            <div class="tip-title">Switches</div>
            <div class="tip-row">Click to view all switches.</div>
          </svelte:fragment>
        </HoverTip>

        <HoverTip>
          <a href="/admin/server-groups" class="kpi-tile accent-warning">
            <span class="kpi-icon">
              <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z" />
              </svg>
            </span>
            <span class="kpi-text">
              <span class="kpi-value">{serverGroups.length}</span>
              <span class="kpi-label">Server groups</span>
            </span>
          </a>
          <svelte:fragment slot="content">
            <div class="tip-title">Server groups</div>
            <div class="tip-row">Click to view all groups.</div>
          </svelte:fragment>
        </HoverTip>

        <HoverTip>
          <a href="/admin/services" class="kpi-tile accent-danger">
            <span class="kpi-icon">
              <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
              </svg>
            </span>
            <span class="kpi-text">
              <span class="kpi-value">{services.length}</span>
              <span class="kpi-label">Services</span>
            </span>
          </a>
          <svelte:fragment slot="content">
            <div class="tip-title">Services</div>
            {#if services.length}
              {#each Object.entries(servicesByStatus) as [status, count]}
                <div class="tip-row">{count} {status}</div>
              {/each}
            {:else}
              <div class="tip-row">No services yet.</div>
            {/if}
          </svelte:fragment>
        </HoverTip>
      </div>

      <!-- Location health: one summary + a simple per-location chip -->
      <section class="panel health-panel">
        <div class="panel-header">
          <h2>Location health</h2>
          <span
            class="health-summary"
            class:health-ok={locations.length > 0 && issueLocations.length === 0}
            class:health-warn={issueLocations.length > 0}
          >
            {healthLoading ? 'Checking…' : healthySummary}
          </span>
        </div>
        {#if sectionErrors.locations}
          <Alert type="warning">Couldn't load locations.</Alert>
        {:else if locations.length === 0}
          <p class="panel-empty">No locations configured yet.</p>
        {:else}
          <div class="location-chip-row">
            {#each locationHealth as loc}
              <HoverTip>
                <a
                  href={`/admin/locations/${loc.id}`}
                  class="location-chip"
                  class:chip-healthy={loc.health === 'healthy'}
                  class:chip-issues={loc.health === 'issues'}
                  class:chip-unconfigured={loc.health === 'unconfigured'}
                >
                  <span class="chip-dot" />
                  <span class="chip-name">{loc.name}</span>
                </a>
                <svelte:fragment slot="content">
                  <div class="tip-title">{loc.name}</div>
                  <div class="tip-row">DHCP: {statusLabel(loc.dhcp)}</div>
                  <div class="tip-row">TFTP: {statusLabel(loc.tftp)}</div>
                </svelte:fragment>
              </HoverTip>
            {/each}
          </div>
        {/if}
      </section>

      <!-- Domain panels: everything else, one glance per area -->
      <div class="domain-grid">
        <div class="domain-panel accent-accent">
          <div class="domain-header">
            <a href="/admin/services" class="domain-title">Services</a>
            <span class="domain-count">{services.length}</span>
          </div>
          <div class="domain-body">
            {#if services.length}
              {#each Object.entries(servicesByStatus) as [status, count]}
                <span class="status-chip status-{status}">{count} {status}</span>
              {/each}
            {:else}
              <span class="domain-empty">No services yet</span>
            {/if}
          </div>
        </div>

        <div class="domain-panel accent-info">
          <div class="domain-header">
            <span class="domain-title-static">Clients &amp; admins</span>
          </div>
          <div class="domain-body">
            <a href="/admin/users" class="domain-stat"><strong>{clients.length}</strong> clients</a>
            <a href="/admin/admins" class="domain-stat"><strong>{admins.length}</strong> admins</a>
          </div>
        </div>

        <div class="domain-panel accent-warning">
          <div class="domain-header">
            <a href="/admin/proxmox-inventory" class="domain-title">Proxmox</a>
          </div>
          <div class="domain-body">
            <a href="/admin/proxmox-inventory" class="domain-stat"><strong>{proxmoxClusters.length}</strong> clusters</a>
          </div>
        </div>

        <div class="domain-panel accent-success">
          <div class="domain-header">
            <span class="domain-title-static">IPAM &amp; proxy</span>
          </div>
          <div class="domain-body">
            <a href="/admin/proxy-ipam" class="domain-stat"><strong>{ipamSubnets.length}</strong> subnets</a>
            <a href="/admin/vm-ip-allocations" class="domain-stat"><strong>{vmIpAllocations.length}</strong> VM IPs</a>
          </div>
        </div>

        <div class="domain-panel accent-secondary">
          <div class="domain-header">
            <a href="/admin/billing-integrations" class="domain-title">Billing</a>
          </div>
          <div class="domain-body">
            <a href="/admin/billing-integrations" class="domain-stat"
              ><strong>{billingIntegrations.length}</strong> integrations</a
            >
          </div>
        </div>

        <div class="domain-panel accent-neutral">
          <div class="domain-header">
            <span class="domain-title-static">Bare-metal tooling</span>
          </div>
          <div class="domain-body">
            <a href="/admin/os-templates" class="domain-stat"><strong>{osTemplates.length}</strong> OS templates</a>
            <a href="/admin/plugins" class="domain-stat"><strong>{plugins.length}</strong> plugins</a>
          </div>
        </div>
      </div>
    </div>

    <!-- Aggregate traffic: fills the remaining viewport height -->
    <section class="panel traffic-shell">
      <AggregateTrafficPanel {locations} {switches} {serverGroups} />
    </section>
  </div>
{/if}

<style>
  .dashboard-loading {
    display: flex;
    justify-content: center;
    padding: 80px 0;
  }

  .dashboard {
    display: flex;
    flex-direction: column;
    gap: 20px;
    flex: 1;
    min-height: 0;
    height: 100%;
  }

  .dashboard-top {
    display: flex;
    flex-direction: column;
    gap: 20px;
    flex-shrink: 0;
  }

  /* KPI strip */
  .kpi-strip {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
    gap: 12px;
  }

  .kpi-tile {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 14px 16px;
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-sm);
    text-decoration: none;
    position: relative;
    overflow: hidden;
    transition: border-color 0.15s ease;
  }

  .kpi-tile::before {
    content: '';
    position: absolute;
    left: 0;
    top: 8px;
    bottom: 8px;
    width: 2px;
    background: var(--tile-accent, var(--accent-color));
  }

  .kpi-tile:hover {
    border-color: color-mix(in srgb, var(--tile-accent, var(--accent-color)) 45%, var(--border-color));
  }

  .kpi-icon {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 32px;
    height: 32px;
    flex-shrink: 0;
    border-radius: var(--radius-sm);
    background: color-mix(in srgb, var(--tile-accent, var(--accent-color)) 16%, transparent);
    color: var(--tile-accent, var(--accent-color));
  }

  .kpi-icon svg {
    width: 20px;
    height: 20px;
  }

  .kpi-text {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }

  .kpi-value {
    font-size: 23px;
    font-weight: 700;
    letter-spacing: -0.03em;
    color: var(--text-primary);
    line-height: 1.1;
  }

  .kpi-label {
    font-size: 12px;
    color: var(--text-secondary);
    font-weight: 500;
    letter-spacing: 0.01em;
  }

  /* Accent palette shared by KPI tiles and domain panels */
  .accent-accent {
    --tile-accent: var(--accent-color);
  }
  .accent-info {
    --tile-accent: var(--info-color);
  }
  .accent-success {
    --tile-accent: var(--success-color);
  }
  .accent-warning {
    --tile-accent: var(--warning-color);
  }
  .accent-danger {
    --tile-accent: var(--danger-color);
  }
  .accent-secondary {
    --tile-accent: var(--secondary-color);
  }
  .accent-neutral {
    --tile-accent: var(--primary-light);
  }

  /* Shared panel shell */
  .panel {
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-sm);
    padding: 14px 16px;
  }

  .panel-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    flex-wrap: wrap;
    margin-bottom: 16px;
  }

  .panel-header h2 {
    margin: 0;
    font-size: 15px;
    font-weight: 700;
    letter-spacing: -0.02em;
    color: var(--text-primary);
  }

  .panel-empty {
    margin: 0;
    color: var(--text-secondary);
    font-size: 14px;
  }

  .health-summary {
    font-size: 12px;
    font-weight: 600;
    padding: 2px 8px;
    border-radius: 3px;
    background: var(--bg-tertiary);
    color: var(--text-secondary);
  }

  .health-summary.health-ok {
    background: var(--success-bg);
    color: var(--success-text);
  }

  .health-summary.health-warn {
    background: var(--danger-bg);
    color: var(--danger-text);
  }

  /* Location chips */
  .location-chip-row {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
  }

  .location-chip {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 5px 10px;
    border-radius: var(--radius-sm);
    border: 1px solid var(--border-color);
    background: var(--bg-secondary);
    text-decoration: none;
    color: var(--text-primary);
    font-size: 12.5px;
    font-weight: 600;
    transition: border-color 0.15s ease;
  }

  .location-chip:hover {
    border-color: var(--accent-color);
  }

  .chip-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--text-tertiary);
    flex-shrink: 0;
  }

  .chip-healthy .chip-dot {
    background: var(--success-color);
  }

  .chip-issues .chip-dot {
    background: var(--danger-color);
  }

  .chip-unconfigured .chip-dot {
    background: var(--text-tertiary);
  }

  /* Domain panels */
  .domain-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 16px;
  }

  .domain-panel {
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-sm);
    padding: 12px 14px 12px 16px;
    display: flex;
    flex-direction: column;
    gap: 8px;
    position: relative;
    overflow: hidden;
  }

  .domain-panel::before {
    content: '';
    position: absolute;
    left: 0;
    top: 8px;
    bottom: 8px;
    width: 2px;
    background: var(--tile-accent, var(--accent-color));
  }

  .domain-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }

  .domain-title {
    font-size: 14px;
    font-weight: 700;
    color: var(--text-primary);
    text-decoration: none;
  }

  .domain-title:hover {
    color: var(--accent-color);
  }

  .domain-title-static {
    font-size: 14px;
    font-weight: 700;
    color: var(--text-primary);
  }

  .domain-count {
    font-size: 14px;
    font-weight: 700;
    color: var(--text-secondary);
  }

  .domain-body {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }

  .domain-empty {
    font-size: 13px;
    color: var(--text-tertiary);
  }

  .domain-stat {
    font-size: 13px;
    color: var(--text-secondary);
    text-decoration: none;
  }

  .domain-stat strong {
    color: var(--text-primary);
    font-weight: 700;
  }

  .domain-stat:hover {
    color: var(--accent-color);
  }

  .domain-stat:hover strong {
    color: var(--accent-color);
  }

  .status-chip {
    padding: 2px 7px;
    border-radius: 3px;
    font-size: 11.5px;
    font-weight: 600;
    text-transform: capitalize;
    background: var(--bg-tertiary);
    color: var(--text-secondary);
  }

  .status-chip.status-active {
    background: var(--success-bg);
    color: var(--success-text);
  }

  .status-chip.status-pending {
    background: var(--info-bg);
    color: var(--info-text);
  }

  .status-chip.status-suspended {
    background: var(--warning-bg);
    color: var(--warning-text);
  }

  .status-chip.status-terminated {
    background: var(--danger-bg);
    color: var(--danger-text);
  }

  /* Traffic panel shell — flexes to fill the remaining viewport height */
  .traffic-shell {
    flex: 1;
    display: flex;
    flex-direction: column;
    min-height: 380px;
  }

  /* Hover tip content typography (rendered inside HoverTip's popover) */
  .tip-title {
    font-weight: 700;
    color: var(--text-primary);
    margin-bottom: 6px;
  }

  .tip-row {
    color: var(--text-secondary);
    font-size: 12.5px;
  }

  @media (max-width: 768px) {
    .kpi-strip {
      grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    }

    .panel {
      padding: 16px;
    }

    .traffic-shell {
      min-height: 320px;
    }
  }
</style>
