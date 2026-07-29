<script>
  import { onMount } from 'svelte';
  import {
    getResellerPanelClient,
    listResellerPanelClients,
  } from '../../lib/api.js';
  import { Alert, Spinner } from '../ui/index.js';

  let clients = [];
  let selected;
  let loading = true;
  let detailLoading = false;
  let error = '';

  onMount(async () => {
    try {
      clients = await listResellerPanelClients();
    } catch (err) {
      error = err.message || 'Clients could not be loaded.';
    } finally {
      loading = false;
    }
  });

  async function openClient(id) {
    detailLoading = true;
    error = '';
    try {
      selected = await getResellerPanelClient(id);
    } catch (err) {
      error = err.message || 'Client details could not be loaded.';
    } finally {
      detailLoading = false;
    }
  }
</script>

<section aria-labelledby="clients-title">
  <div class="heading"><h1 id="clients-title">Clients</h1><p>Downstream users created through your reseller API.</p></div>
  {#if error}<Alert type="danger">{error}</Alert>{/if}
  {#if loading}
    <div class="state"><Spinner /><span>Loading clients…</span></div>
  {:else if clients.length === 0}
    <div class="empty">No downstream clients have been created.</div>
  {:else}
    <div class="layout">
      <div class="table-wrap">
        <table>
          <thead><tr><th>Client</th><th>External identity</th><th>Services</th><th></th></tr></thead>
          <tbody>
            {#each clients as client (client.id)}
              <tr>
                <td><strong>{client.username}</strong><small>{client.email}</small></td>
                <td>{client.external_username || client.external_email || client.external_user_id || '—'}</td>
                <td>{client.service_count}</td>
                <td><button on:click={() => openClient(client.id)}>View</button></td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
      {#if selected || detailLoading}
        <aside>
          {#if detailLoading}<Spinner />{:else}
            <button class="close" on:click={() => (selected = null)} aria-label="Close client details">×</button>
            <h2>{selected.username}</h2>
            <p>{selected.email}</p>
            <dl>
              <div><dt>External ID</dt><dd>{selected.external_user_id || '—'}</dd></div>
              <div><dt>External user</dt><dd>{selected.external_username || '—'}</dd></div>
              <div><dt>Created</dt><dd>{new Date(selected.created_at).toLocaleString()}</dd></div>
            </dl>
            <h3>Services ({selected.service_count})</h3>
            {#if selected.services.length}
              <ul>{#each selected.services as service}<li><span><strong>{service.name}</strong><small>{service.product_code || service.service_type}</small></span><span class="badge">{service.status}</span></li>{/each}</ul>
            {:else}<p class="muted">This client has no services.</p>{/if}
          {/if}
        </aside>
      {/if}
    </div>
  {/if}
</section>

<style>
  .heading { margin-bottom: 22px; }.heading h1 { margin: 0 0 6px; font-size: 32px; }.heading p { margin: 0; color: var(--text-secondary); }.state { min-height: 220px; display: grid; place-items: center; align-content: center; gap: 12px; }.empty { padding: 30px; border: 1px dashed var(--border-color); border-radius: var(--radius-lg); color: var(--text-tertiary); text-align: center; }
  .layout { display: grid; grid-template-columns: minmax(0, 1fr) 340px; gap: 18px; align-items: start; }.table-wrap { overflow-x: auto; border: 1px solid var(--border-color); border-radius: var(--radius-lg); background: var(--bg-primary); }table { width: 100%; border-collapse: collapse; }th, td { padding: 13px 15px; border-bottom: 1px solid var(--border-color); text-align: left; }th { color: var(--text-tertiary); font-size: 11px; text-transform: uppercase; letter-spacing: .05em; }td { color: var(--text-secondary); }td strong { color: var(--text-primary); }td small { display: block; margin-top: 3px; color: var(--text-tertiary); }td button { border: 1px solid var(--border-color); border-radius: 7px; padding: 7px 10px; background: var(--bg-secondary); color: var(--portal-accent); font-weight: 700; cursor: pointer; }
  aside { position: relative; padding: 20px; border: 1px solid var(--border-color); border-radius: var(--radius-lg); background: var(--bg-primary); box-shadow: var(--shadow-sm); }aside h2 { margin: 0 30px 4px 0; }aside > p { margin: 0; color: var(--text-secondary); }.close { position: absolute; top: 12px; right: 12px; border: 0; background: none; color: var(--text-tertiary); font-size: 24px; cursor: pointer; }dl { display: grid; gap: 9px; margin: 18px 0; }dl div { padding: 9px; border-radius: 7px; background: var(--bg-secondary); }dt { color: var(--text-tertiary); font-size: 11px; }dd { margin: 3px 0 0; overflow-wrap: anywhere; }h3 { font-size: 14px; }ul { list-style: none; margin: 0; padding: 0; }li { display: flex; justify-content: space-between; gap: 10px; padding: 10px 0; border-bottom: 1px solid var(--border-color); }li small { display: block; color: var(--text-tertiary); }.badge { height: fit-content; padding: 4px 7px; border-radius: 999px; background: var(--portal-accent-soft); color: var(--portal-accent); text-transform: capitalize; font-size: 11px; }.muted { color: var(--text-tertiary); }
  @media (max-width: 900px) { .layout { grid-template-columns: 1fr; } }
</style>
