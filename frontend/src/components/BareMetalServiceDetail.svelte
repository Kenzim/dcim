<script>
  import { onMount } from 'svelte';
  import PageHeader from './PageHeader.svelte';
  import { navigate } from '../lib/router.js';
  import { getBareMetalService, deleteServiceCompletely, updateAdminServiceStatus } from '../lib/api.js';
  import ServerDetail from './ServerDetail.svelte';

  export let serviceId;

  let service = null;
  let loading = true;
  let error = null;
  let busy = false;
  let statusDraft = 'pending';

  async function load() {
    loading = true;
    error = null;
    try {
      service = await getBareMetalService(serviceId);
      statusDraft = service?.status || 'pending';
    } catch (e) {
      error = e.message || String(e);
    } finally {
      loading = false;
    }
  }

  onMount(load);

  async function deleteService() {
    if (!service?.id) return;
    if (!confirm(`Delete ${service.name} completely? This is permanent.`)) return;
    busy = true;
    error = null;
    try {
      await deleteServiceCompletely(service.id);
      navigate('/admin/bare-metal-services');
    } catch (e) {
      error = e.message || String(e);
    } finally {
      busy = false;
    }
  }

  async function saveStatus() {
    if (!service?.id) return;
    busy = true;
    error = null;
    try {
      service = await updateAdminServiceStatus(service.id, statusDraft);
      statusDraft = service.status;
    } catch (e) {
      error = e.message || String(e);
    } finally {
      busy = false;
    }
  }

  async function terminateService() {
    if (!service?.id) return;
    if (!confirm(`Set ${service.name} to terminated?`)) return;
    statusDraft = 'terminated';
    await saveStatus();
  }
</script>

<PageHeader title="Bare Metal Service Detail" />
<div class="container">
  <button class="btn-secondary" on:click={() => navigate('/admin/bare-metal-services')}>Back to Bare Metal Services</button>
  {#if loading}
    <p>Loading...</p>
  {:else if error}
    <p class="error">{error}</p>
  {:else if service}
    <section class="panel">
      <h3>Service</h3>
      <table class="kv-table">
        <tbody>
          <tr><th>Name</th><td>{service.name}</td></tr>
          <tr><th>Status</th><td>{service.status}</td></tr>
          <tr><th>Owner</th><td>{service.owner_username || 'Unassigned'}</td></tr>
          <tr><th>Server</th><td>{service.server_name || '—'} (id: {service.server_id ?? '—'})</td></tr>
          <tr><th>Source</th><td>{service.provisioning_source || 'billing'}</td></tr>
        </tbody>
      </table>
      <div class="status-editor">
        <label>
          Service status
          <select bind:value={statusDraft} disabled={busy}>
            <option value="pending">pending</option>
            <option value="active">active</option>
            <option value="suspended">suspended</option>
            <option value="terminated">terminated</option>
          </select>
        </label>
        <button class="btn-secondary" disabled={busy || statusDraft === service.status} on:click={saveStatus}>
          Save Status
        </button>
        <button class="btn-danger" disabled={busy} on:click={terminateService}>Terminate Service</button>
      </div>
      <button class="btn-danger" disabled={busy} on:click={deleteService}>Delete Service Completely</button>
    </section>
    {#if service.server_id}
      <section class="panel">
        <h3>Hardware Control</h3>
        <ServerDetail serverId={service.server_id} onBack={() => {}} />
      </section>
    {/if}
  {/if}
</div>

<style>
  .container { padding: 32px; display: grid; gap: 16px; }
  .panel { border: 1px solid var(--border-color); border-radius: 10px; padding: 16px; background: var(--bg-secondary); }
  .status-editor { display: flex; flex-wrap: wrap; gap: 8px; align-items: end; margin-bottom: 12px; }
  .status-editor label { display: flex; flex-direction: column; gap: 4px; font-size: 13px; color: var(--text-secondary); font-weight: 600; }
  .status-editor select { padding: 8px 10px; border-radius: 8px; border: 1px solid var(--border-color); min-width: 180px; }
  .kv-table { width: 100%; border-collapse: collapse; margin-bottom: 12px; }
  .kv-table th, .kv-table td { padding: 8px 10px; border-bottom: 1px solid var(--border-color); text-align: left; vertical-align: top; }
  .kv-table th { width: 180px; color: var(--text-secondary); font-weight: 700; }
  .kv-table tr:last-child th, .kv-table tr:last-child td { border-bottom: none; }
  .error { color: var(--danger-color); }
  .btn-secondary { padding: 8px 12px; border: 1px solid var(--border-color); border-radius: 8px; background: var(--bg-tertiary); cursor: pointer; width: fit-content; }
  .btn-danger { padding: 8px 12px; border: 0; border-radius: 8px; background: var(--danger-color); color: white; cursor: pointer; width: fit-content; }
</style>
