<script>
  import { onMount } from 'svelte';
  import { Alert, Button, Spinner } from '../../ui/index.js';
  import * as api from '../../../lib/vmFirewallApi.js';
  import { STATE_INFO } from '../../../lib/firewallText.js';
  import FirewallStateBadge from '../FirewallStateBadge.svelte';

  let rows = [];
  let locations = [];
  let loading = true;
  let error = '';
  let state = '';
  let locationId = '';
  let q = '';

  const locId = (loc) => loc.location_id ?? loc.id;
  const locName = (loc) => loc.name ?? loc.location_name ?? `Location ${locId(loc)}`;
  const idOf = (row) => row.service_id ?? row.id;
  const when = (iso) => (iso ? new Date(iso).toLocaleString() : '');

  async function load() {
    loading = true;
    error = '';
    try {
      rows = await api.listStatus({ state, locationId, q: q.trim() });
    } catch (err) {
      error = err.message || 'Failed to load the status list';
    } finally {
      loading = false;
    }
  }

  onMount(async () => {
    locations = await api.listLocations().catch(() => []);
    await load();
  });
</script>

<section class="panel">
  <form class="filters" on:submit|preventDefault={load}>
    <label>State
      <select bind:value={state} on:change={load}>
        <option value="">All states</option>
        {#each Object.entries(STATE_INFO) as [key, info]}<option value={key}>{info.label}</option>{/each}
      </select>
    </label>
    <label>Location
      <select bind:value={locationId} on:change={load}>
        <option value="">All locations</option>
        {#each locations as loc}<option value={String(locId(loc))}>{locName(loc)}</option>{/each}
      </select>
    </label>
    <label class="grow">Search
      <input type="search" bind:value={q} placeholder="Service name, owner or id" />
    </label>
    <Button type="submit" variant="secondary" disabled={loading}>Search</Button>
  </form>

  {#if error}<Alert type="error">{error}</Alert>{/if}

  {#if loading}
    <div class="loading"><Spinner /><span>Loading…</span></div>
  {:else if rows.length === 0}
    <p class="hint">No servers match.</p>
  {:else}
    <div class="table-wrap">
      <table>
        <thead>
          <tr><th>Server</th><th>Owner</th><th>Location</th><th>State</th><th>Customer firewall</th><th>Last applied</th><th>Last error</th></tr>
        </thead>
        <tbody>
          {#each rows as row (idOf(row))}
            <tr>
              <td><a href="/admin/services/{idOf(row)}">{row.service_name ?? row.name ?? `#${idOf(row)}`}</a></td>
              <td>{row.owner_username ?? row.owner ?? ''}</td>
              <td>{row.location_name ?? row.location ?? ''}</td>
              <td><FirewallStateBadge state={row.state} /></td>
              <td>{row.client_enabled ? 'On' : 'Off'}</td>
              <td>{when(row.last_applied_at)}</td>
              <td class="err">{row.last_error || ''}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
    <p class="hint">{rows.length} {rows.length === 1 ? 'server' : 'servers'}.</p>
  {/if}
</section>

<style>
  .panel { display: flex; flex-direction: column; gap: 12px; border: 1px solid var(--border-color); border-radius: 12px; padding: 16px 18px; background: var(--bg-primary); }
  .filters { display: flex; flex-wrap: wrap; gap: 12px; align-items: end; }
  label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; font-weight: 600; color: var(--text-secondary); }
  .grow { flex: 1; min-width: 200px; }
  input, select { padding: 7px 9px; border: 1px solid var(--border-color); border-radius: 6px; background: var(--bg-primary); color: var(--text-primary); font-size: 13px; }
  .loading { display: flex; align-items: center; gap: 10px; color: var(--text-secondary); }
  .hint { margin: 0; font-size: 12px; color: var(--text-tertiary); }
  .table-wrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; padding: 8px 10px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--text-tertiary); background: var(--bg-secondary); white-space: nowrap; }
  td { padding: 8px 10px; border-top: 1px solid var(--border-color); }
  td a { color: var(--accent-color); }
  .err { color: var(--danger-text); max-width: 320px; overflow-wrap: anywhere; }
</style>
