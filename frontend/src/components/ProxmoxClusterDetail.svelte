<script>
  import { onMount } from 'svelte';
  import PageHeader from './PageHeader.svelte';
  import Button from './ui/Button.svelte';
  import Spinner from './ui/Spinner.svelte';
  import { navigate } from '../lib/router.js';
  import { getProxmoxClusterOverview, syncProxmoxCluster } from '../lib/api.js';

  export let clusterId;

  let loading = true;
  let syncing = false;
  let error = '';
  let overview = null;

  async function load() {
    loading = true;
    error = '';
    try {
      overview = await getProxmoxClusterOverview(clusterId);
    } catch (err) {
      error = err.message;
    } finally {
      loading = false;
    }
  }

  async function runSync() {
    syncing = true;
    error = '';
    try {
      await syncProxmoxCluster(clusterId);
      await load();
    } catch (err) {
      error = err.message;
    } finally {
      syncing = false;
    }
  }

  function formatBytes(n) {
    if (n === null || n === undefined) return '—';
    if (n >= 1e12) return (n / 1e12).toFixed(2) + ' TB';
    if (n >= 1e9) return (n / 1e9).toFixed(2) + ' GB';
    if (n >= 1e6) return (n / 1e6).toFixed(2) + ' MB';
    if (n >= 1e3) return (n / 1e3).toFixed(2) + ' KB';
    return `${n} B`;
  }

  function pct(used, total) {
    if (!total) return null;
    return Math.min(100, Math.round((used / total) * 1000) / 10);
  }

  function formatDate(iso) {
    if (!iso) return 'never';
    return new Date(iso).toLocaleString();
  }

  onMount(load);

  $: cpuPct = overview?.totals ? pct(overview.totals.cpu_used_cores, overview.totals.cpu_total_cores) : null;
  $: ramPct = overview?.totals ? pct(overview.totals.ram_used_bytes, overview.totals.ram_total_bytes) : null;
  // "Storage" on the capacity snapshot is the node's root filesystem (OS disk), not
  // VM storage pool capacity — surface both since root-disk-full and pool-full mean
  // very different things operationally.
  $: rootDiskPct = overview?.totals ? pct(overview.totals.storage_used_bytes, overview.totals.storage_total_bytes) : null;
  $: storagePools = overview
    ? overview.nodes.flatMap((n) => n.storages || []).reduce(
        (acc, s) => ({
          total: acc.total + (s.total_bytes || 0),
          used: acc.used + (s.used_bytes || 0),
          any: acc.any || s.total_bytes != null,
        }),
        { total: 0, used: 0, any: false }
      )
    : { total: 0, used: 0, any: false };
  $: storagePoolPct = storagePools.any ? pct(storagePools.used, storagePools.total) : null;
</script>

<PageHeader title={overview?.cluster?.name ? `Proxmox Cluster: ${overview.cluster.name}` : 'Proxmox Cluster'} />
<div class="page">
  <button class="back-link" on:click={() => navigate('/admin/proxmox-inventory')}>&larr; Back to Proxmox Inventory</button>

  {#if error}<div class="error">{error}</div>{/if}

  {#if loading}
    <div class="loading-row"><Spinner /> <span>Loading cluster overview…</span></div>
  {:else if overview}
    <section class="panel">
      <div class="panel-head">
        <div>
          <h3>{overview.cluster.name}</h3>
          <p class="mono meta">{overview.cluster.api_url}</p>
        </div>
        <div class="panel-actions">
          <span class="status-chip" class:enabled={overview.cluster.enabled} class:disabled={!overview.cluster.enabled}>
            {overview.cluster.enabled ? 'Enabled' : 'Disabled'}
          </span>
          <Button size="small" variant="secondary" on:click={runSync} disabled={syncing}>
            {syncing ? 'Syncing…' : 'Sync now'}
          </Button>
        </div>
      </div>
      <div class="identity-grid">
        <div><span class="label">VMID range</span><span>{overview.cluster.vmid_min ?? '—'} – {overview.cluster.vmid_max ?? '—'}</span></div>
        <div><span class="label">Verify SSL</span><span>{overview.cluster.verify_ssl ? 'Yes' : 'No'}</span></div>
        <div><span class="label">Nodes</span><span>{overview.nodes.length}</span></div>
        <div><span class="label">Templates</span><span>{overview.template_count}</span></div>
        <div><span class="label">Storages</span><span>{overview.storage_count}</span></div>
      </div>
    </section>

    <div class="fact-grid">
      <section class="fact">
        <h3>CPU</h3>
        {#if overview.totals}
          <p class="fact-value">{overview.totals.cpu_used_cores.toFixed(1)} / {overview.totals.cpu_total_cores.toFixed(0)} cores</p>
          <div class="bar"><div class="bar-fill" style="width: {cpuPct ?? 0}%"></div></div>
          <p class="fact-sub">{cpuPct ?? 0}% used</p>
        {:else}
          <p class="fact-value muted">No data yet</p>
        {/if}
      </section>
      <section class="fact">
        <h3>RAM</h3>
        {#if overview.totals}
          <p class="fact-value">{formatBytes(overview.totals.ram_used_bytes)} / {formatBytes(overview.totals.ram_total_bytes)}</p>
          <div class="bar"><div class="bar-fill" style="width: {ramPct ?? 0}%"></div></div>
          <p class="fact-sub">{ramPct ?? 0}% used</p>
        {:else}
          <p class="fact-value muted">No data yet</p>
        {/if}
      </section>
      <section class="fact">
        <h3>Root Disk</h3>
        {#if overview.totals}
          <p class="fact-value">{formatBytes(overview.totals.storage_used_bytes)} / {formatBytes(overview.totals.storage_total_bytes)}</p>
          <div class="bar"><div class="bar-fill" style="width: {rootDiskPct ?? 0}%"></div></div>
          <p class="fact-sub">{rootDiskPct ?? 0}% used · OS disk, not VM storage</p>
        {:else}
          <p class="fact-value muted">No data yet</p>
        {/if}
      </section>
      <section class="fact">
        <h3>Storage Pools</h3>
        {#if storagePools.any}
          <p class="fact-value">{formatBytes(storagePools.used)} / {formatBytes(storagePools.total)}</p>
          <div class="bar"><div class="bar-fill" style="width: {storagePoolPct ?? 0}%"></div></div>
          <p class="fact-sub">{storagePoolPct ?? 0}% used · across all node storages</p>
        {:else}
          <p class="fact-value muted">No data yet</p>
        {/if}
      </section>
    </div>

    {#if !overview.totals}
      <p class="muted empty-note">No capacity data yet — click "Sync now" to pull live stats from Proxmox.</p>
    {/if}

    <section class="panel">
      <div class="panel-head">
        <h3>Nodes</h3>
        <p>Per-node capacity, templates, and storages from the last sync.</p>
      </div>
      {#if overview.nodes.length === 0}
        <p class="muted empty-note">No nodes synced yet.</p>
      {:else}
        <div class="node-grid">
          {#each overview.nodes as node (node.id)}
            {@const nodeCpuPct = node.capacity ? pct(node.capacity.cpu_used * node.capacity.cpu_total, node.capacity.cpu_total) : null}
            {@const nodeRamPct = node.capacity ? pct(node.capacity.ram_used_bytes, node.capacity.ram_total_bytes) : null}
            {@const nodeStoragePct = node.capacity ? pct(node.capacity.storage_used_bytes, node.capacity.storage_total_bytes) : null}
            <div class="node-card">
              <div class="node-card-head">
                <strong>{node.node_name}</strong>
                <span class="status-chip" class:enabled={node.enabled} class:disabled={!node.enabled}>
                  {node.enabled ? 'Enabled' : 'Disabled'}
                </span>
              </div>
              {#if node.capacity}
                <div class="node-metric">
                  <span class="label">CPU</span>
                  <span>{(node.capacity.cpu_used * node.capacity.cpu_total).toFixed(1)} / {node.capacity.cpu_total?.toFixed(0) ?? '—'} cores</span>
                  <div class="bar small"><div class="bar-fill" style="width: {nodeCpuPct ?? 0}%"></div></div>
                </div>
                <div class="node-metric">
                  <span class="label">RAM</span>
                  <span>{formatBytes(node.capacity.ram_used_bytes)} / {formatBytes(node.capacity.ram_total_bytes)}</span>
                  <div class="bar small"><div class="bar-fill" style="width: {nodeRamPct ?? 0}%"></div></div>
                </div>
                <div class="node-metric">
                  <span class="label">Storage</span>
                  <span>{formatBytes(node.capacity.storage_used_bytes)} / {formatBytes(node.capacity.storage_total_bytes)}</span>
                  <div class="bar small"><div class="bar-fill" style="width: {nodeStoragePct ?? 0}%"></div></div>
                </div>
                <p class="last-synced">Last synced: {formatDate(node.capacity.created_at)}</p>
              {:else}
                <p class="muted">No capacity data yet.</p>
              {/if}
              <div class="node-detail-list">
                <span class="label">Templates ({node.template_count})</span>
                {#if node.templates.length}
                  <ul>
                    {#each node.templates as tpl}
                      <li class="mono">{tpl.vmid}: {tpl.name}</li>
                    {/each}
                  </ul>
                {:else}
                  <span class="muted">None</span>
                {/if}
              </div>
              <div class="node-detail-list">
                <span class="label">Storages ({node.storage_count})</span>
                {#if node.storages.length}
                  <ul>
                    {#each node.storages as storage}
                      <li>
                        {storage.storage_name}{storage.storage_type ? ` (${storage.storage_type})` : ''}
                        {#if storage.total_bytes}
                          — {formatBytes(storage.used_bytes)} / {formatBytes(storage.total_bytes)}
                        {/if}
                      </li>
                    {/each}
                  </ul>
                {:else}
                  <span class="muted">None</span>
                {/if}
              </div>
            </div>
          {/each}
        </div>
      {/if}
    </section>
  {/if}
</div>

<style>
  .page { padding: 24px; display: flex; flex-direction: column; gap: 16px; }
  @media (max-width: 768px) { .page { padding: 16px; } }
  .back-link { border: none; background: none; color: var(--accent-color); font-weight: 600; font-size: 13px; cursor: pointer; padding: 0; width: fit-content; }
  .back-link:hover { text-decoration: underline; }
  .error { color: var(--danger-color); }
  .loading-row { display: flex; align-items: center; gap: 10px; color: var(--text-secondary); padding: 24px 0; }
  .muted { color: var(--text-secondary); }
  .empty-note { font-size: 13px; }

  .panel {
    border: 1px solid var(--border-color);
    border-radius: 12px;
    padding: 16px 18px;
    background: var(--bg-primary);
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .panel-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
  .panel-head h3 { margin: 0 0 4px; font-size: 1rem; }
  .panel-head p { margin: 0; font-size: 13px; color: var(--text-secondary); }
  .panel-actions { display: flex; align-items: center; gap: 10px; }
  .meta { margin: 0; color: var(--text-secondary); font-size: 12px; }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; }

  .status-chip {
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    padding: 3px 8px;
    border-radius: 6px;
  }
  .status-chip.enabled { background: color-mix(in srgb, #22c55e 18%, transparent); color: #22c55e; }
  .status-chip.disabled { background: color-mix(in srgb, var(--danger-color) 16%, transparent); color: var(--danger-color); }

  .identity-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; }
  .identity-grid > div { display: flex; flex-direction: column; gap: 2px; }
  .label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.03em; color: var(--text-tertiary); }

  .fact-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }
  @media (max-width: 640px) { .fact-grid { grid-template-columns: 1fr; } }
  .fact { border: 1px solid var(--border-color); border-radius: 12px; padding: 14px 16px; background: var(--bg-primary); display: flex; flex-direction: column; gap: 8px; }
  .fact h3 { margin: 0; font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; color: var(--text-tertiary); }
  .fact-value { margin: 0; font-size: 18px; font-weight: 700; color: var(--text-primary); }
  .fact-value.muted { font-size: 13px; font-weight: 500; }
  .fact-sub { margin: 0; font-size: 11px; color: var(--text-secondary); }

  .bar { height: 6px; border-radius: 999px; background: var(--bg-tertiary); overflow: hidden; }
  .bar.small { height: 5px; }
  .bar-fill { height: 100%; background: var(--accent-color); border-radius: 999px; }

  .node-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 12px; }
  .node-card { border: 1px solid var(--border-color); border-radius: 10px; padding: 12px 14px; display: flex; flex-direction: column; gap: 8px; }
  .node-card-head { display: flex; align-items: center; justify-content: space-between; }
  .node-metric { display: flex; flex-direction: column; gap: 3px; font-size: 12px; }
  .node-metric .label { font-size: 10px; }
  .last-synced { margin: 0; font-size: 11px; color: var(--text-tertiary); }
  .node-detail-list { display: flex; flex-direction: column; gap: 4px; font-size: 12px; }
  .node-detail-list ul { margin: 0; padding-left: 16px; display: flex; flex-direction: column; gap: 2px; color: var(--text-secondary); }
</style>
