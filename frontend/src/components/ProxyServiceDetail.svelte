<script>
  import { onMount } from 'svelte';
  import PageHeader from './PageHeader.svelte';
  import { navigate } from '../lib/router.js';
  import {
    getService,
    deleteServiceCompletely,
    updateAdminServiceStatus,
    listServiceIpAssignments,
    assignIpamAddress,
    releaseIpamAssignment,
    rotateIpamAssignment,
  } from '../lib/api.js';
  import ServicePermissionsPanel from './ServicePermissionsPanel.svelte';

  export let serviceId;

  const PROXY_PORT = 8080;

  let service = null;
  let assignments = [];
  let loading = true;
  let error = null;
  let busy = false;
  let statusDraft = 'pending';
  let copyAllMsg = '';

  async function load() {
    loading = true;
    error = null;
    try {
      [service, assignments] = await Promise.all([
        getService(serviceId),
        listServiceIpAssignments(serviceId),
      ]);
      statusDraft = service?.status || 'pending';
    } catch (e) {
      error = e.message || String(e);
    } finally {
      loading = false;
    }
  }

  onMount(load);
  $: serviceId, load();

  function formatDate(dateString) {
    if (!dateString) return 'N/A';
    return new Date(dateString).toLocaleString();
  }

  function proxyUrl(scheme, a) {
    if (!a?.ip_address) return '';
    const creds = a.username && a.password ? `${a.username}:${a.password}@` : '';
    return `${scheme}://${creds}${a.ip_address}:${PROXY_PORT}`;
  }

  function endpointLine(a) {
    if (!a?.ip_address || !a.username || !a.password) return '';
    const port = a.port || PROXY_PORT;
    return `${a.ip_address}:${port}:${a.username}:${a.password}`;
  }

  function allEndpointLines() {
    return assignments.map(endpointLine).filter(Boolean).join('\n');
  }

  async function copyAllEndpoints() {
    const text = allEndpointLines();
    if (!text) return;
    copyAllMsg = '';
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.setAttribute('readonly', '');
        ta.style.position = 'fixed';
        ta.style.left = '-9999px';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
      }
      copyAllMsg = 'Copied';
      setTimeout(() => { copyAllMsg = ''; }, 2000);
    } catch (e) {
      error = e.message || 'Failed to copy';
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
    if (!confirm(`Set ${service.name} to terminated? This releases all assigned IPs.`)) return;
    statusDraft = 'terminated';
    await saveStatus();
    await load();
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

  async function assignAnotherIp() {
    if (!service?.id) return;
    busy = true;
    error = null;
    try {
      await assignIpamAddress({ service_id: service.id });
      await load();
    } catch (e) {
      error = e.message || String(e);
    } finally {
      busy = false;
    }
  }

  async function releaseAssignment(assignment) {
    if (!confirm(`Release ${assignment.ip_address}? The customer will lose access to this proxy IP immediately.`)) return;
    busy = true;
    error = null;
    try {
      await releaseIpamAssignment(assignment.id);
      await load();
    } catch (e) {
      error = e.message || String(e);
    } finally {
      busy = false;
    }
  }

  async function rotateAssignment(assignment) {
    if (!confirm(`Rotate credentials for ${assignment.ip_address}? The old username/password stop working immediately.`)) return;
    busy = true;
    error = null;
    try {
      await rotateIpamAssignment(assignment.id);
      await load();
    } catch (e) {
      error = e.message || String(e);
    } finally {
      busy = false;
    }
  }
</script>

<PageHeader title="Proxy Service Detail" />
<div class="container">
  <button class="btn-secondary" on:click={() => navigate('/admin/services')}>Back to Services</button>
  {#if loading}
    <p>Loading...</p>
  {:else if error && !service}
    <p class="error">{error}</p>
  {:else if service}
    {#if error}<p class="error">{error}</p>{/if}
    <section class="panel">
      <h3>Service</h3>
      <table class="kv-table">
        <tbody>
          <tr><th>Name</th><td>{service.name}</td></tr>
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
          <tr><th>Product</th><td>{service.product_code || '—'}</td></tr>
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

    <section class="panel">
      <div class="panel-head">
        <h3>Assigned proxy IPs</h3>
        <div class="panel-actions">
          {#if assignments.length > 0}
            <button class="btn-secondary" disabled={busy} on:click={copyAllEndpoints}>
              {copyAllMsg || 'Copy all (ip:port:user:pass)'}
            </button>
          {/if}
          <button class="btn-secondary" disabled={busy} on:click={assignAnotherIp}>+ Assign another IP</button>
        </div>
      </div>
      {#if assignments.length === 0}
        <p class="muted">No IPs assigned yet — service stays pending until at least one is assigned.</p>
      {:else}
        <table class="kv-table assignments-table">
          <thead>
            <tr>
              <th>IP</th>
              <th>Username</th>
              <th>Password</th>
              <th>HTTP URL</th>
              <th>SOCKS5 URL</th>
              <th>Assigned</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {#each assignments as a (a.id)}
              <tr>
                <td class="mono">{a.ip_address}</td>
                <td class="mono">{a.username || '—'}</td>
                <td class="mono">{a.password || '—'}</td>
                <td class="mono small">{proxyUrl('http', a)}</td>
                <td class="mono small">{proxyUrl('socks5', a)}</td>
                <td>{formatDate(a.assigned_at)}</td>
                <td class="row-actions">
                  <button class="btn-secondary" disabled={busy} on:click={() => rotateAssignment(a)}>Rotate</button>
                  <button class="btn-danger" disabled={busy} on:click={() => releaseAssignment(a)}>Release</button>
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      {/if}
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

<style>
  .container { padding: 32px; display: grid; gap: 16px; }
  @media (max-width: 768px) { .container { padding: 16px; } }
  .panel { border: 1px solid var(--border-color); border-radius: 10px; padding: 16px; background: var(--bg-secondary); }
  .panel-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 4px; flex-wrap: wrap; }
  .panel-actions { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  .kv-table { width: 100%; border-collapse: collapse; margin-bottom: 12px; }
  .kv-table th { text-align: left; padding: 6px 10px; color: var(--text-secondary); font-weight: 600; }
  .kv-table td { padding: 6px 10px; color: var(--text-primary); }
  .assignments-table th { white-space: nowrap; }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; }
  .mono.small { font-size: 11px; max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
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
  .row-actions { display: flex; gap: 6px; white-space: nowrap; }
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
