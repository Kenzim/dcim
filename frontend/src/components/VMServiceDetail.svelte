<script>
  import { onMount } from 'svelte';
  import PageHeader from './PageHeader.svelte';
  import { navigate } from '../lib/router.js';
  import {
    getVmService,
    provisionVmService,
    vmPowerAction,
    destroyVmGuest,
    recreateVmGuest,
    deleteServiceCompletely,
    updateAdminServiceStatus,
  } from '../lib/api.js';

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
      service = await getVmService(serviceId);
      statusDraft = service?.status || 'pending';
    } catch (e) {
      error = e.message || String(e);
    } finally {
      loading = false;
    }
  }

  async function act(fn) {
    if (!service?.id) return;
    busy = true;
    error = null;
    try {
      service = await fn(service.id);
    } catch (e) {
      error = e.message || String(e);
    } finally {
      busy = false;
    }
  }

  async function deleteService() {
    if (!service?.id) return;
    if (!confirm(`Delete ${service.name} completely? This is permanent.`)) return;
    busy = true;
    error = null;
    try {
      await deleteServiceCompletely(service.id);
      navigate('/admin/vm-services');
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

  async function destroyVm() {
    if (!service?.id) return;
    if (!confirm('Stop and destroy VM guest? Service and VMID reservation stay attached to this service.')) return;
    await act((id) => destroyVmGuest(id));
  }

  onMount(load);
</script>

<PageHeader title="VM Service Detail" />
<div class="container">
  <button class="btn-secondary" on:click={() => navigate('/admin/vm-services')}>Back to VM Services</button>
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
          <tr><th>Source</th><td>{service.provisioning_source || 'billing'}</td></tr>
          <tr><th>Placement</th><td>cluster {service.proxmox_cluster_id ?? '—'} / {service.proxmox_node_name || '—'} / {service.proxmox_vmid ?? '—'}</td></tr>
          <tr><th>Assigned VM IP</th><td>{service.vm_ip_address || '—'}</td></tr>
          <tr><th>Guest state</th><td>{service.vm_guest_state || 'unprovisioned'}</td></tr>
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
    </section>

    <section class="panel">
      <h3>VM Control</h3>
      <div class="actions">
        <button class="btn-primary" disabled={busy} on:click={() => act((id) => provisionVmService(id))}>Provision VM</button>
        <button class="btn-secondary" disabled={busy} on:click={() => act((id) => vmPowerAction(id, 'on'))}>Power On</button>
        <button class="btn-secondary" disabled={busy} on:click={() => act((id) => vmPowerAction(id, 'off'))}>Power Off</button>
        <button class="btn-secondary" disabled={busy} on:click={() => act((id) => vmPowerAction(id, 'reboot'))}>Reboot</button>
        <button class="btn-secondary" disabled={busy} on:click={destroyVm}>Stop + Destroy VM</button>
        <button class="btn-secondary" disabled={busy} on:click={() => act((id) => recreateVmGuest(id))}>Recreate VM</button>
        <button class="btn-danger" disabled={busy} on:click={deleteService}>Delete Service Completely</button>
      </div>
    </section>
  {/if}
</div>

<style>
  .container { padding: 32px; display: grid; gap: 16px; }
  .panel { border: 1px solid var(--border-color); border-radius: 10px; padding: 16px; background: var(--bg-secondary); }
  .actions { display: flex; flex-wrap: wrap; gap: 8px; }
  .status-editor { display: flex; flex-wrap: wrap; gap: 8px; align-items: end; margin-top: 12px; }
  .status-editor label { display: flex; flex-direction: column; gap: 4px; font-size: 13px; color: var(--text-secondary); font-weight: 600; }
  .status-editor select { padding: 8px 10px; border-radius: 8px; border: 1px solid var(--border-color); min-width: 180px; }
  .kv-table { width: 100%; border-collapse: collapse; }
  .kv-table th, .kv-table td { padding: 8px 10px; border-bottom: 1px solid var(--border-color); text-align: left; vertical-align: top; }
  .kv-table th { width: 180px; color: var(--text-secondary); font-weight: 700; }
  .kv-table tr:last-child th, .kv-table tr:last-child td { border-bottom: none; }
  .error { color: var(--danger-color); }
  .btn-primary { padding: 8px 12px; border: 0; border-radius: 8px; background: var(--accent-color); color: white; cursor: pointer; }
  .btn-secondary { padding: 8px 12px; border: 1px solid var(--border-color); border-radius: 8px; background: var(--bg-tertiary); cursor: pointer; }
  .btn-danger { padding: 8px 12px; border: 0; border-radius: 8px; background: var(--danger-color); color: white; cursor: pointer; }
</style>
