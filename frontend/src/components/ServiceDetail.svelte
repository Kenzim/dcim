<script>
  import { onMount } from 'svelte';
  import PageHeader from './PageHeader.svelte';
  import { navigate } from '../lib/router.js';
  import { getService, deleteServiceCompletely, updateAdminServiceStatus } from '../lib/api.js';
  import BareMetalServiceDetail from './BareMetalServiceDetail.svelte';
  import VMServiceDetail from './VMServiceDetail.svelte';
  import ProxyServiceDetail from './ProxyServiceDetail.svelte';
  import ServicePermissionsPanel from './ServicePermissionsPanel.svelte';

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
      service = await getService(serviceId);
      statusDraft = service?.status || 'pending';
    } catch (e) {
      error = e.message || String(e);
    } finally {
      loading = false;
    }
  }

  onMount(load);
  // Reload when navigating directly between two service detail pages.
  $: serviceId, load();

  function formatDate(dateString) {
    if (!dateString) return 'N/A';
    return new Date(dateString).toLocaleString();
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

  async function deleteService() {
    if (!service?.id) return;
    if (!confirm(`Delete ${service.name} completely? This is permanent.`)) return;
    busy = true;
    error = null;
    try {
      await deleteServiceCompletely(service.id);
      navigate('/admin/services');
    } catch (e) {
      error = e.message || String(e);
    } finally {
      busy = false;
    }
  }
</script>

{#if loading}
  <PageHeader title="Service Detail" />
  <div class="container"><p>Loading...</p></div>
{:else if error && !service}
  <PageHeader title="Service Detail" />
  <div class="container">
    <button class="btn-secondary" on:click={() => navigate('/admin/services')}>Back to Services</button>
    <p class="error">{error}</p>
  </div>
{:else if service?.service_type === 'bare_metal'}
  <BareMetalServiceDetail {serviceId} />
{:else if service?.service_type === 'vm'}
  <VMServiceDetail {serviceId} />
{:else if service?.service_type === 'http_proxy'}
  <ProxyServiceDetail {serviceId} />
{:else}
  <!-- Generic fallback for any other/unknown service type. -->
  <PageHeader title="Service Detail" />
  <div class="container">
    <button class="btn-secondary" on:click={() => navigate('/admin/services')}>Back to Services</button>
    {#if error}<p class="error">{error}</p>{/if}
    {#if service}
      <section class="panel">
        <h3>Service</h3>
        <table class="kv-table">
          <tbody>
            <tr><th>Name</th><td>{service.name}</td></tr>
            <tr><th>Type</th><td>{service.service_type || '—'}</td></tr>
            <tr><th>Status</th><td>{service.status}</td></tr>
            <tr><th>Owner</th><td>{service.owner_username || 'Unassigned'}</td></tr>
            <tr>
              <th>Billing owner</th>
              <td>
                {#if service.external_user_id}
                  {service.external_username || 'WHMCS user'}
                  {#if service.external_user_external_id}
                    <span class="muted">(external id: {service.external_user_external_id})</span>
                  {/if}
                {:else}
                  Unassigned
                {/if}
              </td>
            </tr>
            <tr><th>Source</th><td>{service.provisioning_source || 'billing'}</td></tr>
            {#if service.description}
              <tr><th>Description</th><td>{service.description}</td></tr>
            {/if}
            <tr><th>Created</th><td>{formatDate(service.created_at)}</td></tr>
            <tr><th>Updated</th><td>{formatDate(service.updated_at)}</td></tr>
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
      <ServicePermissionsPanel
        serviceId={service.id}
        serviceType={service.service_type}
        permissionSetId={service.permission_set_id}
        permissionOverrides={service.permission_overrides}
        on:saved={load}
      />
    {/if}
  </div>
{/if}

<style>
  .container { padding: 32px; display: grid; gap: 16px; }
  @media (max-width: 768px) { .container { padding: 16px; } }
  .panel { border: 1px solid var(--border-color); border-radius: 10px; padding: 16px; background: var(--bg-secondary); }
  .kv-table { width: 100%; border-collapse: collapse; margin-bottom: 12px; }
  .kv-table th { text-align: left; padding: 6px 10px; color: var(--text-secondary); font-weight: 600; width: 200px; }
  .kv-table td { padding: 6px 10px; color: var(--text-primary); }
  .muted { color: var(--text-secondary); }
  .status-editor { display: flex; flex-wrap: wrap; gap: 8px; align-items: end; margin-bottom: 12px; }
  .status-editor label { display: flex; flex-direction: column; gap: 4px; font-size: 13px; color: var(--text-secondary); font-weight: 600; }
  .status-editor select {
    min-width: 180px;
    padding: 8px 32px 8px 10px;
    border-radius: 8px;
    border: 1px solid var(--border-color);
    background-color: var(--bg-primary);
    color: var(--text-primary);
    appearance: none;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%23475569' d='M6 8L1 3h10z'/%3E%3C/svg%3E");
    background-repeat: no-repeat;
    background-position: right 10px center;
    background-size: 12px;
  }
  .btn-secondary {
    padding: 10px 16px;
    background: var(--bg-tertiary);
    color: var(--text-primary);
    border: none;
    border-radius: 8px;
    font-weight: 600;
    cursor: pointer;
  }
  .btn-danger {
    padding: 10px 16px;
    background: var(--danger-color);
    color: white;
    border: none;
    border-radius: 8px;
    font-weight: 600;
    cursor: pointer;
  }
  .btn-danger:disabled, .btn-secondary:disabled { opacity: 0.6; cursor: not-allowed; }
  .error { color: var(--danger-color); }
</style>
