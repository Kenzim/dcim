<script>
  import PageHeader from './PageHeader.svelte';
  import { 
    getDHCPStatus, 
    startDHCPServer, 
    stopDHCPServer, 
    restartDHCPServer,
    getDHCPConfig,
    updateDHCPConfig,
    regenerateDHCPConfig,
    getTFTPStatus,
    startFTPServer,
    stopFTPServer,
    restartFTPServer,
    getTFTPConfig,
    updateTFTPConfig
  } from '../lib/api.js';
  import { onMount } from 'svelte';

  export let onNavigate = null;

  let loading = true;
  let error = null;
  let dhcpStatus = null;
  let dhcpConfig = null;
  let tftpStatus = null;
  let tftpConfig = null;
  let saving = false;
  let actionInProgress = false;
  let regenerating = false;
  let savingTFTP = false;
  let actionInProgressTFTP = false;

  let formData = {
    enabled: true,
    interfaces: [{ interface: 'eth1', ip: '192.168.12.74' }],
    hand_out_leases: true,
    default_lease_time: 3600,
    max_lease_time: 7200,
    config_file_path: '/root/dcim/dhcpd.conf',
    lease_file_path: '/root/dcim/dhcpd.leases'
  };

  let tftpFormData = {
    enabled: true,
    root_directory: '/root/dcim/tftp',
    bind_address: '192.168.12.74',
    bind_port: 69,
    allow_create: true,
    verbose: true,
    ipv4_only: true
  };

  onMount(async () => {
    await loadData();
  });

  async function loadData() {
    try {
      loading = true;
      error = null;
      [dhcpStatus, dhcpConfig, tftpStatus, tftpConfig] = await Promise.all([
        getDHCPStatus(),
        getDHCPConfig(),
        getTFTPStatus(),
        getTFTPConfig()
      ]);
      
      // Populate form with config
      if (dhcpConfig) {
        formData = {
          enabled: dhcpConfig.enabled,
          interfaces: dhcpConfig.interfaces || [{ interface: 'eth1', ip: '192.168.12.74' }],
          hand_out_leases: dhcpConfig.hand_out_leases,
          default_lease_time: dhcpConfig.default_lease_time,
          max_lease_time: dhcpConfig.max_lease_time,
          config_file_path: dhcpConfig.config_file_path,
          lease_file_path: dhcpConfig.lease_file_path
        };
      }

      if (tftpConfig) {
        tftpFormData = {
          enabled: tftpConfig.enabled,
          root_directory: tftpConfig.root_directory,
          bind_address: tftpConfig.bind_address,
          bind_port: tftpConfig.bind_port,
          allow_create: tftpConfig.allow_create,
          verbose: tftpConfig.verbose,
          ipv4_only: tftpConfig.ipv4_only
        };
      }
    } catch (err) {
      error = err.message;
      console.error('Failed to load services data:', err);
    } finally {
      loading = false;
    }
  }

  async function handleStart() {
    try {
      actionInProgress = true;
      await startDHCPServer();
      await loadData();
    } catch (err) {
      alert('Failed to start DHCP server: ' + err.message);
    } finally {
      actionInProgress = false;
    }
  }

  async function handleStop() {
    if (!confirm('Stop the DHCP server?')) return;
    try {
      actionInProgress = true;
      await stopDHCPServer();
      await loadData();
    } catch (err) {
      alert('Failed to stop DHCP server: ' + err.message);
    } finally {
      actionInProgress = false;
    }
  }

  async function handleRestart() {
    if (!confirm('Restart the DHCP server?')) return;
    try {
      actionInProgress = true;
      await restartDHCPServer();
      await loadData();
    } catch (err) {
      alert('Failed to restart DHCP server: ' + err.message);
    } finally {
      actionInProgress = false;
    }
  }

  async function handleRegenerate() {
    if (!confirm('Regenerate DHCP configuration from current server settings?')) return;
    try {
      regenerating = true;
      error = null;
      await regenerateDHCPConfig();
      await loadData();
      alert('DHCP configuration regenerated successfully.');
    } catch (err) {
      error = err.message;
      alert('Failed to regenerate DHCP configuration: ' + err.message);
    } finally {
      regenerating = false;
    }
  }

  function addInterface() {
    formData.interfaces = [...formData.interfaces, { interface: '', ip: '' }];
  }

  function removeInterface(index) {
    formData.interfaces = formData.interfaces.filter((_, i) => i !== index);
  }

  async function handleSave() {
    try {
      saving = true;
      error = null;
      
      await updateDHCPConfig(formData);
      
      // Reload to get updated status
      await loadData();
      
      alert('DHCP configuration saved successfully. DHCP server will be restarted if running.');
    } catch (err) {
      error = err.message;
      alert('Failed to save configuration: ' + err.message);
    } finally {
      saving = false;
    }
  }

  function formatLeaseTime(seconds) {
    if (seconds < 60) return `${seconds}s`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
    return `${Math.floor(seconds / 3600)}h`;
  }

  async function handleStartTFTP() {
    try {
      actionInProgressTFTP = true;
      await startFTPServer();
      await loadData();
    } catch (err) {
      alert('Failed to start TFTP server: ' + err.message);
    } finally {
      actionInProgressTFTP = false;
    }
  }

  async function handleStopTFTP() {
    if (!confirm('Stop the TFTP server?')) return;
    try {
      actionInProgressTFTP = true;
      await stopFTPServer();
      await loadData();
    } catch (err) {
      alert('Failed to stop TFTP server: ' + err.message);
    } finally {
      actionInProgressTFTP = false;
    }
  }

  async function handleRestartTFTP() {
    if (!confirm('Restart the TFTP server?')) return;
    try {
      actionInProgressTFTP = true;
      await restartFTPServer();
      await loadData();
    } catch (err) {
      alert('Failed to restart TFTP server: ' + err.message);
    } finally {
      actionInProgressTFTP = false;
    }
  }

  async function handleSaveTFTP() {
    try {
      savingTFTP = true;
      error = null;
      
      await updateTFTPConfig(tftpFormData);
      
      // Reload to get updated status
      await loadData();
      
      alert('TFTP configuration saved successfully. TFTP server will be restarted if running.');
    } catch (err) {
      error = err.message;
      alert('Failed to save configuration: ' + err.message);
    } finally {
      savingTFTP = false;
    }
  }
</script>

<div class="services-page">
  <PageHeader title="Services" onNavigate={onNavigate} />

  {#if loading}
    <div class="content-body">
      <div class="loading">Loading services...</div>
    </div>
  {:else if error}
    <div class="content-body">
      <div class="error">Error: {error}</div>
      <button class="btn-primary" on:click={loadData}>Retry</button>
    </div>
  {:else}
    <div class="content-body">
      <!-- DHCP Server Section -->
      <div class="card">
        <div class="card-header">
          <h2>DHCP Server</h2>
          <div class="card-actions">
            {#if dhcpStatus && dhcpStatus.running}
              <span class="status-badge enabled">Running</span>
              <button class="btn-secondary" on:click={handleStop} disabled={actionInProgress}>
                Stop
              </button>
              <button class="btn-secondary" on:click={handleRestart} disabled={actionInProgress}>
                Restart
              </button>
            {:else}
              <span class="status-badge disabled">Stopped</span>
              <button class="btn-primary" on:click={handleStart} disabled={actionInProgress}>
                Start
              </button>
            {/if}
            <button class="btn-secondary" on:click={handleRegenerate} disabled={regenerating || actionInProgress}>
              {regenerating ? 'Regenerating...' : 'Regenerate Config'}
            </button>
          </div>
        </div>
        <div class="card-body">
          {#if dhcpStatus}
            <div class="info-grid">
              <div class="info-item">
                <label>Status</label>
                <span class="status-badge" class:enabled={dhcpStatus.running} class:disabled={!dhcpStatus.running}>
                  {dhcpStatus.status}
                </span>
              </div>
              {#if dhcpStatus.pid}
                <div class="info-item">
                  <label>PID</label>
                  <span>{dhcpStatus.pid}</span>
                </div>
              {/if}
              <div class="info-item">
                <label>Interface</label>
                <span>{dhcpStatus.interface || 'N/A'}</span>
              </div>
              <div class="info-item">
                <label>Config File</label>
                <span>{dhcpStatus.config_path || 'N/A'}</span>
              </div>
            </div>
          {/if}

          <div class="form-section" style="margin-top: 2rem;">
            <h3>Configuration</h3>
            
            <div class="form-group">
              <label>
                <input type="checkbox" bind:checked={formData.enabled} />
                Enable DHCP Server
              </label>
            </div>

            <div class="form-group">
              <label>
                <input type="checkbox" bind:checked={formData.hand_out_leases} />
                Hand Out Normal DHCP Leases
              </label>
              <small class="field-help">If disabled, DHCP will only serve PXE boot requests</small>
            </div>

            <div class="form-row">
              <div class="form-group">
                <label>Default Lease Time (seconds)</label>
                <input type="number" bind:value={formData.default_lease_time} min="60" step="60" />
                <small class="field-help">Current: {formatLeaseTime(formData.default_lease_time)}</small>
              </div>
              <div class="form-group">
                <label>Max Lease Time (seconds)</label>
                <input type="number" bind:value={formData.max_lease_time} min="60" step="60" />
                <small class="field-help">Current: {formatLeaseTime(formData.max_lease_time)}</small>
              </div>
            </div>

            <div class="form-group">
              <label>Network Interfaces</label>
              <div class="interfaces-list">
                {#each formData.interfaces as iface, index}
                  <div class="interface-item">
                    <div class="form-row">
                      <div class="form-group">
                        <label>Interface</label>
                        <input type="text" bind:value={iface.interface} placeholder="eth1" />
                      </div>
                      <div class="form-group">
                        <label>IP Address</label>
                        <input type="text" bind:value={iface.ip} placeholder="192.168.12.74" />
                      </div>
                      <div class="form-group" style="flex: 0 0 auto; padding-top: 1.5rem;">
                        <button type="button" class="btn-secondary btn-small" on:click={() => removeInterface(index)}>
                          Remove
                        </button>
                      </div>
                    </div>
                  </div>
                {/each}
                <button type="button" class="btn-secondary" on:click={addInterface}>
                  Add Interface
                </button>
              </div>
            </div>

            <div class="form-row">
              <div class="form-group">
                <label>Config File Path</label>
                <input type="text" bind:value={formData.config_file_path} />
              </div>
              <div class="form-group">
                <label>Lease File Path</label>
                <input type="text" bind:value={formData.lease_file_path} />
              </div>
            </div>

            <div class="form-actions">
              <button class="btn-primary" on:click={handleSave} disabled={saving}>
                {saving ? 'Saving...' : 'Save Configuration'}
              </button>
            </div>
          </div>
        </div>
      </div>

      <!-- TFTP Server Section -->
      <div class="card">
        <div class="card-header">
          <h2>TFTP Server</h2>
          <div class="card-actions">
            {#if tftpStatus && tftpStatus.running}
              <span class="status-badge enabled">Running</span>
              <button class="btn-secondary" on:click={handleStopTFTP} disabled={actionInProgressTFTP}>
                Stop
              </button>
              <button class="btn-secondary" on:click={handleRestartTFTP} disabled={actionInProgressTFTP}>
                Restart
              </button>
            {:else}
              <span class="status-badge disabled">Stopped</span>
              <button class="btn-primary" on:click={handleStartTFTP} disabled={actionInProgressTFTP}>
                Start
              </button>
            {/if}
          </div>
        </div>
        <div class="card-body">
          {#if tftpStatus}
            <div class="info-grid">
              <div class="info-item">
                <label>Status</label>
                <span class="status-badge" class:enabled={tftpStatus.running} class:disabled={!tftpStatus.running}>
                  {tftpStatus.status}
                </span>
              </div>
              {#if tftpStatus.pid}
                <div class="info-item">
                  <label>PID</label>
                  <span>{tftpStatus.pid}</span>
                </div>
              {:else if tftpStatus.running}
                <div class="info-item">
                  <label>PID</label>
                  <span class="text-muted">Unknown</span>
                </div>
              {/if}
              <div class="info-item">
                <label>Root Directory</label>
                <span>{tftpConfig?.root_directory || tftpStatus.root_directory || 'N/A'}</span>
              </div>
              <div class="info-item">
                <label>Bind Address</label>
                <span>{tftpConfig?.bind_address || tftpStatus.bind_address || 'N/A'}:{tftpConfig?.bind_port || tftpStatus.bind_port || 'N/A'}</span>
              </div>
              <div class="info-item">
                <label>IPv4 Only</label>
                <span>{tftpConfig?.ipv4_only ?? tftpFormData?.ipv4_only ?? false ? 'Yes' : 'No'}</span>
              </div>
              <div class="info-item">
                <label>Allow Create</label>
                <span>{tftpConfig?.allow_create ?? tftpFormData?.allow_create ?? false ? 'Yes' : 'No'}</span>
              </div>
              <div class="info-item">
                <label>Verbose Logging</label>
                <span>{tftpConfig?.verbose ?? tftpFormData?.verbose ?? false ? 'Yes' : 'No'}</span>
              </div>
            </div>
          {/if}

          <div class="form-section" style="margin-top: 2rem;">
            <h3>Configuration</h3>
            
            <div class="form-group">
              <label>
                <input type="checkbox" bind:checked={tftpFormData.enabled} />
                Enable TFTP Server
              </label>
            </div>

            <div class="form-row">
              <div class="form-group">
                <label>Root Directory</label>
                <input type="text" bind:value={tftpFormData.root_directory} />
                <small class="field-help">TFTP root directory (chroot)</small>
              </div>
              <div class="form-group">
                <label>Bind Address</label>
                <input type="text" bind:value={tftpFormData.bind_address} placeholder="192.168.12.74" />
                <small class="field-help">IP address to bind to</small>
              </div>
              <div class="form-group">
                <label>Bind Port</label>
                <input type="number" bind:value={tftpFormData.bind_port} min="1" max="65535" />
                <small class="field-help">Port to bind to (default: 69)</small>
              </div>
            </div>

            <div class="form-group">
              <label>
                <input type="checkbox" bind:checked={tftpFormData.allow_create} />
                Allow File Creation
              </label>
              <small class="field-help">Allow clients to create/write files</small>
            </div>

            <div class="form-group">
              <label>
                <input type="checkbox" bind:checked={tftpFormData.verbose} />
                Verbose Logging
              </label>
              <small class="field-help">Enable verbose logging</small>
            </div>

            <div class="form-group">
              <label>
                <input type="checkbox" bind:checked={tftpFormData.ipv4_only} />
                IPv4 Only
              </label>
              <small class="field-help">Disable IPv6 support</small>
            </div>

            <div class="form-actions">
              <button class="btn-primary" on:click={handleSaveTFTP} disabled={savingTFTP}>
                {savingTFTP ? 'Saving...' : 'Save Configuration'}
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  {/if}
</div>

<style>
  .services-page {
    display: flex;
    flex-direction: column;
    height: 100vh;
  }

  .content-body {
    flex: 1;
    overflow-y: auto;
    padding: 2rem;
  }

  .card {
    background: white;
    border-radius: 8px;
    box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
    margin-bottom: 2rem;
  }

  .card-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 1.5rem;
    border-bottom: 1px solid #e5e7eb;
  }

  .card-header h2 {
    margin: 0;
    font-size: 1.5rem;
    font-weight: 600;
  }

  .card-actions {
    display: flex;
    gap: 0.5rem;
    align-items: center;
  }

  .card-body {
    padding: 1.5rem;
  }

  .info-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 1rem;
    margin-bottom: 1rem;
  }

  .info-item {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
  }

  .info-item label {
    font-weight: 500;
    color: #6b7280;
    font-size: 0.875rem;
  }

  .info-item span {
    color: #111827;
  }

  .text-muted {
    color: #6b7280;
    font-style: italic;
  }

  .status-badge {
    padding: 0.25rem 0.75rem;
    border-radius: 4px;
    font-size: 0.875rem;
    font-weight: 500;
  }

  .status-badge.enabled {
    background: #d1fae5;
    color: #065f46;
  }

  .status-badge.disabled {
    background: #fee2e2;
    color: #991b1b;
  }

  .form-section {
    margin-top: 1rem;
  }

  .form-section h3 {
    margin: 0 0 1rem 0;
    font-size: 1.25rem;
    font-weight: 600;
  }

  .form-group {
    margin-bottom: 1rem;
  }

  .form-group label {
    display: block;
    margin-bottom: 0.5rem;
    font-weight: 500;
    color: #374151;
  }

  .form-group input[type="text"],
  .form-group input[type="number"] {
    width: 100%;
    padding: 0.5rem;
    border: 1px solid #d1d5db;
    border-radius: 4px;
    font-size: 1rem;
  }

  .form-group input[type="checkbox"] {
    margin-right: 0.5rem;
  }

  .form-row {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 1rem;
  }

  .field-help {
    display: block;
    margin-top: 0.25rem;
    font-size: 0.875rem;
    color: #6b7280;
  }

  .interfaces-list {
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }

  .interface-item {
    padding: 1rem;
    background: #f9fafb;
    border-radius: 4px;
    border: 1px solid #e5e7eb;
  }

  .form-actions {
    margin-top: 1.5rem;
    padding-top: 1.5rem;
    border-top: 1px solid #e5e7eb;
  }

  .btn-primary, .btn-secondary {
    padding: 0.5rem 1rem;
    border-radius: 4px;
    font-weight: 500;
    cursor: pointer;
    border: none;
    transition: all 0.2s;
  }

  .btn-primary {
    background: #3b82f6;
    color: white;
  }

  .btn-primary:hover:not(:disabled) {
    background: #2563eb;
  }

  .btn-secondary {
    background: #e5e7eb;
    color: #374151;
  }

  .btn-secondary:hover:not(:disabled) {
    background: #d1d5db;
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
    color: #dc2626;
  }
</style>
