<script>
  import { onMount } from 'svelte';
  import { getResellerPanelQuotas } from '../../lib/api.js';
  import { Alert, Spinner } from '../ui/index.js';

  let quotas = [];
  let loading = true;
  let error = '';
  const labels = {
    services: 'Services',
    cpu_cores: 'CPU cores',
    ram_mb: 'RAM (MB)',
    disk_gb: 'Disk (GB)',
  };

  onMount(async () => {
    try {
      quotas = await getResellerPanelQuotas();
    } catch (err) {
      error = err.message || 'Stock usage could not be loaded.';
    } finally {
      loading = false;
    }
  });

  function percent(used, limit) {
    if (limit === null || limit === undefined) return 0;
    if (limit === 0) return used > 0 ? 100 : 0;
    return Math.min(100, Math.round((used / limit) * 100));
  }
</script>

<section aria-labelledby="stock-title">
  <div class="heading"><h1 id="stock-title">Stock & quota usage</h1><p>Effective reseller and group limits after override resolution.</p></div>
  {#if error}<Alert type="danger">{error}</Alert>{/if}
  {#if loading}
    <div class="state"><Spinner /><span>Loading quotas…</span></div>
  {:else if quotas.length === 0}
    <div class="empty">No effective quotas are allocated. New provisioning remains default-deny.</div>
  {:else}
    <div class="quota-grid">
      {#each quotas as quota (quota.id)}
        <article class:warning={quota.warning}>
          <header>
            <div><span>{quota.scope_type.replaceAll('_', ' ')}</span><h2>{quota.scope_name}</h2></div>
            <small>{quota.source === 'group' ? 'Inherited from group' : 'Reseller override'}</small>
          </header>
          <div class="resources">
            {#each Object.keys(labels) as key}
              <div class="resource">
                <div class="resource-heading"><span>{labels[key]}</span><strong>{quota.usage[key]} / {quota.limits[key] ?? 'Unlimited'}</strong></div>
                {#if quota.limits[key] !== null}
                  <div class="track" role="progressbar" aria-label={`${labels[key]} usage`} aria-valuenow={quota.usage[key]} aria-valuemin="0" aria-valuemax={quota.limits[key]}>
                    <span class:near={percent(quota.usage[key], quota.limits[key]) >= 80} style={`width:${percent(quota.usage[key], quota.limits[key])}%`}></span>
                  </div>
                  <small>{quota.remaining[key]} remaining</small>
                {:else}
                  <div class="track unlimited"><span></span></div><small>No limit</small>
                {/if}
              </div>
            {/each}
          </div>
        </article>
      {/each}
    </div>
  {/if}
</section>

<style>
  .heading { margin-bottom: 22px; }.heading h1 { margin: 0 0 6px; font-size: 32px; }.heading p { margin: 0; color: var(--text-secondary); }.state { min-height: 220px; display: grid; place-items: center; align-content: center; gap: 12px; }.empty { padding: 30px; border: 1px dashed var(--border-color); border-radius: var(--radius-lg); color: var(--text-tertiary); text-align: center; }
  .quota-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px; }.quota-grid article { padding: 20px; border: 1px solid var(--border-color); border-radius: var(--radius-lg); background: var(--bg-primary); box-shadow: var(--shadow-sm); }.quota-grid article.warning { border-color: var(--warning-color); }.quota-grid header { display: flex; justify-content: space-between; gap: 12px; }.quota-grid header > div > span { color: var(--portal-accent); text-transform: uppercase; font-size: 10px; font-weight: 800; letter-spacing: .08em; }.quota-grid h2 { margin: 4px 0 0; font-size: 19px; }.quota-grid header small { color: var(--text-tertiary); text-align: right; }
  .resources { display: grid; gap: 15px; margin-top: 20px; }.resource-heading { display: flex; justify-content: space-between; gap: 10px; font-size: 13px; }.resource-heading span { color: var(--text-secondary); }.resource-heading strong { color: var(--text-primary); }.track { height: 8px; margin: 7px 0 5px; overflow: hidden; border-radius: 999px; background: var(--bg-tertiary); }.track > span { display: block; height: 100%; border-radius: inherit; background: var(--portal-accent); }.track > span.near { background: var(--warning-color); }.track.unlimited > span { width: 100%; opacity: .22; }.resource > small { color: var(--text-tertiary); }
  @media (max-width: 760px) { .quota-grid { grid-template-columns: 1fr; } }
</style>
