<script>
  import PageHeader from './PageHeader.svelte';
  import {
    getLocation,
    listServiceInstances,
    createServiceInstance,
    deleteServiceInstance,
    getLocationDHCPStatus,
    startLocationDHCP,
    stopLocationDHCP,
    restartLocationDHCP,
    regenerateLocationDHCP,
    getLocationDHCPLogs,
    getLocationTFTPStatus,
    startLocationTFTP,
    stopLocationTFTP,
    restartLocationTFTP,
    getLocationTFTPLogs
  } from '../lib/api.js';
  import { navigate } from '../lib/router.js';
  import { onMount } from 'svelte';

  export let locationId;
  export let onBack = () => navigate('/admin/locations');

  let location = null;
  let loading = true;
  let error = null;
  let serviceInstances = [];
  let dhcpInstance = null;
  let tftpInstance = null;
  let dhcpStatus = null;
  let tftpStatus = null;
  let dhcpLogs = null;
  let tftpLogs = null;
  let actionInProgress = { dhcp: false, tftp: false, regenerate: false };
  let showAddInstance = false;
  let addInstanceType = 'dhcp';
  let addInstanceName = '';
  let addInstanceUrl = '';
  let addInstanceApiKey = '';
  let addInstanceSaving = false;
  let addInstanceError = null;
  let statusRefreshInterval;

  onMount(async () => {
    await loadData();
    statusRefreshInterval = setInterval(refreshStatus, 8000);
    return () => {
      if (statusRefreshInterval) clearInterval(statusRefreshInterval);
    };
  });

  async function loadData() {
    try {
      loading = true;
      error = null;
      [location, serviceInstances] = await Promise.all([
        getLocation(locationId),
        listServiceInstances(locationId)
      ]);
      dhcpInstance = serviceInstances.find(s => s.service_type === 'dhcp');
      tftpInstance = serviceInstances.find(s => s.service_type === 'tftp');
      await refreshStatus();
    } catch (err) {
      error = err.message;
      console.error('Failed to load location:', err);
    } finally {
      loading = false;
    }
  }

  async function refreshStatus() {
    if (loading || actionInProgress.dhcp || actionInProgress.tftp) return;
    try {
      const promises = [];
      if (dhcpInstance) promises.push(getLocationDHCPStatus(locationId).then(s => { dhcpStatus = s; }).catch(() => { dhcpStatus = { status: 'error', running: false }; }));
      if (tftpInstance) promises.push(getLocationTFTPStatus(locationId).then(s => { tftpStatus = s; }).catch(() => { tftpStatus = { status: 'error', running: false }; }));
      await Promise.all(promises);
    } catch (_) {}
  }

  function openAddInstance(type) {
    addInstanceType = type;
    addInstanceName = location?.name + ' ' + (type === 'dhcp' ? 'DHCP' : 'TFTP') || '';
    addInstanceUrl = '';
    addInstanceApiKey = '';
    addInstanceError = null;
    showAddInstance = true;
  }

  async function handleCreateInstance() {
    if (!addInstanceUrl || !addInstanceApiKey || !addInstanceName) {
      addInstanceError = 'Name, URL, and API key are required';
      return;
    }
    try {
      addInstanceSaving = true;
      addInstanceError = null;
      await createServiceInstance({
        location_id: locationId,
        service_type: addInstanceType,
        name: addInstanceName,
        base_url: addInstanceUrl,
        api_key: addInstanceApiKey
      });
      showAddInstance = false;
      await loadData();
    } catch (err) {
      addInstanceError = err.message;
    } finally {
      addInstanceSaving = false;
    }
  }

  async function handleDeleteInstance(id) {
    if (!confirm('Delete this service instance?')) return;
    try {
      await deleteServiceInstance(id);
      await loadData();
    } catch (err) {
      alert('Failed to delete: ' + err.message);
    }
  }

  async function handleDHCPAction(action) {
    try {
      actionInProgress.dhcp = true;
      if (action === 'start') await startLocationDHCP(locationId);
      else if (action === 'stop') await stopLocationDHCP(locationId);
      else if (action === 'restart') await restartLocationDHCP(locationId);
      await loadData();
    } catch (err) {
      alert(err.message);
    } finally {
      actionInProgress.dhcp = false;
    }
  }

  async function handleRegenerateDHCP() {
    try {
      actionInProgress.regenerate = true;
      await regenerateLocationDHCP(locationId);
      alert('DHCP configuration regenerated successfully.');
      await loadData();
    } catch (err) {
      alert(err.message);
    } finally {
      actionInProgress.regenerate = false;
    }
  }

  async function handleTFTPAction(action) {
    try {
      actionInProgress.tftp = true;
      if (action === 'start') await startLocationTFTP(locationId);
      else if (action === 'stop') await stopLocationTFTP(locationId);
      else if (action === 'restart') await restartLocationTFTP(locationId);
      await loadData();
    } catch (err) {
      alert(err.message);
    } finally {
      actionInProgress.tftp = false;
    }
  }

  async function loadDHCPLogs() {
    try {
      dhcpLogs = await getLocationDHCPLogs(locationId);
    } catch (err) {
      dhcpLogs = { lines: ['Failed to load logs: ' + err.message] };
    }
  }

  async function loadTFTPLogs() {
    try {
      tftpLogs = await getLocationTFTPLogs(locationId);
    } catch (err) {
      tftpLogs = { lines: ['Failed to load logs: ' + err.message] };
    }
  }
</script>

<PageHeader title={location?.name || 'Location'} />

<div class="location-detail">
  <button class="back-button" on:click={onBack}>
    <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 19l-7-7m0 0l7-7m-7 7h18" />
    </svg>
    Back to Locations
  </button>
  {#if loading}
    <div class="loading">Loading...</div>
  {:else if error}
    <div class="error">Error: {error}</div>
    <button class="btn-primary" on:click={loadData}>Retry</button>
  {:else if !location}
    <div class="error">Location not found</div>
  {:else}
    {#if location.description}
      <p class="location-description">{location.description}</p>
    {/if}

    <p class="field-help" style="margin-bottom: 1.5rem;">
      Deploy DHCP/TFTP runners at this location with <code>docker-compose.runners-only.yml</code>, then register instances below.
    </p>

    <div class="cards-grid">
      <!-- DHCP -->
      <div class="card">
        <div class="card-header">
          <h2>DHCP</h2>
          {#if dhcpInstance}
            <div class="card-actions">
              <span class="status-badge" class:enabled={dhcpStatus?.running} class:disabled={!dhcpStatus?.running}>
                {dhcpStatus?.status || 'unknown'}
              </span>
              {#if dhcpStatus?.running}
                <button class="btn-secondary btn-small" on:click={() => handleDHCPAction('stop')} disabled={actionInProgress.dhcp}>Stop</button>
                <button class="btn-secondary btn-small" on:click={() => handleDHCPAction('restart')} disabled={actionInProgress.dhcp}>Restart</button>
              {:else}
                <button class="btn-primary btn-small" on:click={() => handleDHCPAction('start')} disabled={actionInProgress.dhcp}>Start</button>
              {/if}
              <button class="btn-secondary btn-small" on:click={handleRegenerateDHCP} disabled={actionInProgress.regenerate}>
                {actionInProgress.regenerate ? 'Regenerating...' : 'Regenerate'}
              </button>
              <button class="btn-secondary btn-small" on:click={() => handleDeleteInstance(dhcpInstance.id)}>Remove</button>
            </div>
          {:else}
            <button class="btn-secondary btn-small" on:click={() => openAddInstance('dhcp')}>Add DHCP instance</button>
          {/if}
        </div>
        {#if dhcpInstance}
          <div class="card-body">
            <p class="instance-url">{dhcpInstance.base_url}</p>
            <details>
              <summary>View logs</summary>
              <pre class="logs">{dhcpLogs?.lines ? dhcpLogs.lines.join('\n') : 'Click Load logs to fetch'}</pre>
              <button class="btn-secondary btn-small" on:click={loadDHCPLogs}>Load logs</button>
            </details>
          </div>
        {/if}
      </div>

      <!-- TFTP -->
      <div class="card">
        <div class="card-header">
          <h2>TFTP</h2>
          {#if tftpInstance}
            <div class="card-actions">
              <span class="status-badge" class:enabled={tftpStatus?.running} class:disabled={!tftpStatus?.running}>
                {tftpStatus?.status || 'unknown'}
              </span>
              {#if tftpStatus?.running}
                <button class="btn-secondary btn-small" on:click={() => handleTFTPAction('stop')} disabled={actionInProgress.tftp}>Stop</button>
                <button class="btn-secondary btn-small" on:click={() => handleTFTPAction('restart')} disabled={actionInProgress.tftp}>Restart</button>
              {:else}
                <button class="btn-primary btn-small" on:click={() => handleTFTPAction('start')} disabled={actionInProgress.tftp}>Start</button>
              {/if}
              <button class="btn-secondary btn-small" on:click={() => handleDeleteInstance(tftpInstance.id)}>Remove</button>
            </div>
          {:else}
            <button class="btn-secondary btn-small" on:click={() => openAddInstance('tftp')}>Add TFTP instance</button>
          {/if}
        </div>
        {#if tftpInstance}
          <div class="card-body">
            <p class="instance-url">{tftpInstance.base_url}</p>
            <details>
              <summary>View logs</summary>
              <pre class="logs">{tftpLogs?.lines ? tftpLogs.lines.join('\n') : 'Click Load logs to fetch'}</pre>
              <button class="btn-secondary btn-small" on:click={loadTFTPLogs}>Load logs</button>
            </details>
          </div>
        {/if}
      </div>
    </div>

    {#if showAddInstance}
      <div class="modal-overlay" on:click|self={() => showAddInstance = false}>
        <div class="modal-content" on:click|stopPropagation>
          <h3>Add {addInstanceType === 'dhcp' ? 'DHCP' : 'TFTP'} instance</h3>
          {#if addInstanceError}
            <div class="error" style="margin-bottom: 1rem;">{addInstanceError}</div>
          {/if}
          <div class="form-group">
            <label>Name</label>
            <input type="text" bind:value={addInstanceName} placeholder="e.g. US-East DHCP" />
          </div>
          <div class="form-group">
            <label>Base URL</label>
            <input type="text" bind:value={addInstanceUrl} placeholder="http://192.168.1.50:9080" />
          </div>
          <div class="form-group">
            <label>API Key</label>
            <input type="password" bind:value={addInstanceApiKey} placeholder="Set API_KEY on the runner" />
          </div>
          <div class="form-actions">
            <button class="btn-primary" on:click={handleCreateInstance} disabled={addInstanceSaving}>
              {addInstanceSaving ? 'Creating...' : 'Create'}
            </button>
            <button class="btn-secondary" on:click={() => showAddInstance = false}>Cancel</button>
          </div>
        </div>
      </div>
    {/if}
  {/if}
</div>

<style>
  .location-detail {
    padding: 32px;
  }

  .back-button {
    display: inline-flex;
    align-items: center;
    gap: 0.5rem;
    margin-bottom: 1.5rem;
    padding: 0.5rem 1rem;
    background: var(--bg-primary);
    border: 2px solid var(--accent-color);
    border-radius: 8px;
    color: var(--accent-color);
    font-weight: 600;
    cursor: pointer;
    transition: all 0.2s ease;
  }

  .back-button:hover {
    background: var(--accent-color);
    color: white;
  }

  .back-button svg {
    width: 18px;
    height: 18px;
  }

  .location-description {
    color: var(--text-secondary);
    margin: 0 0 1rem 0;
  }

  .field-help {
    font-size: 0.9rem;
    color: var(--text-secondary);
  }

  .field-help code {
    background: var(--bg-tertiary);
    padding: 0.1rem 0.3rem;
    border-radius: 4px;
    font-size: 0.85em;
  }

  .cards-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(380px, 1fr));
    gap: 1.5rem;
  }

  .card {
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: 12px;
    overflow: hidden;
    box-shadow: var(--shadow-md);
  }

  .card-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 0.75rem;
    padding: 1.25rem;
    border-bottom: 1px solid var(--border-color);
  }

  .card-header h2 {
    margin: 0;
    font-size: 1.25rem;
    font-weight: 600;
    color: var(--text-primary);
  }

  .card-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    align-items: center;
  }

  .card-body {
    padding: 1.25rem;
  }

  .instance-url {
    font-size: 0.9rem;
    color: var(--text-secondary);
    margin: 0 0 1rem 0;
  }

  .logs {
    background: var(--bg-tertiary);
    padding: 1rem;
    border-radius: 8px;
    font-size: 0.8rem;
    white-space: pre-wrap;
    max-height: 200px;
    overflow-y: auto;
    margin: 0.5rem 0 0.5rem 0;
  }

  .status-badge {
    padding: 0.25rem 0.75rem;
    border-radius: 4px;
    font-size: 0.875rem;
    font-weight: 500;
  }

  .status-badge.enabled {
    background: var(--success-bg);
    color: var(--success-text);
    border: 1px solid var(--success-color);
  }

  .status-badge.disabled {
    background: var(--danger-bg);
    color: var(--danger-text);
    border: 1px solid var(--danger-color);
  }

  .btn-primary, .btn-secondary {
    padding: 0.5rem 1rem;
    border-radius: 8px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.2s ease;
  }

  .btn-primary {
    background: var(--accent-color);
    color: white;
    border: 1px solid var(--accent-color);
  }

  .btn-primary:hover:not(:disabled) {
    background: var(--accent-dark);
    border-color: var(--accent-dark);
  }

  .btn-secondary {
    background: var(--bg-primary);
    color: var(--text-primary);
    border: 2px solid var(--accent-color);
  }

  .btn-secondary:hover:not(:disabled) {
    background: var(--accent-color);
    color: white;
  }

  .btn-small {
    padding: 0.25rem 0.75rem;
    font-size: 0.875rem;
  }

  button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .loading, .error {
    text-align: center;
    padding: 2rem;
  }

  .error {
    color: var(--danger-color);
  }

  .modal-overlay {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background: var(--overlay-bg);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
  }

  .modal-content {
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: 12px;
    padding: 1.5rem;
    max-width: 480px;
    width: 90%;
    box-shadow: var(--shadow-xl);
  }

  .modal-content h3 {
    margin: 0 0 1rem 0;
    font-size: 1.25rem;
  }

  .form-group {
    margin-bottom: 1rem;
  }

  .form-group label {
    display: block;
    margin-bottom: 0.5rem;
    font-weight: 600;
    color: var(--text-primary);
  }

  .form-group input {
    width: 100%;
    padding: 0.5rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    font-size: 1rem;
    background: var(--bg-primary);
    color: var(--text-primary);
  }

  .form-actions {
    margin-top: 1rem;
    display: flex;
    gap: 0.5rem;
  }
</style>
