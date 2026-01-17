<script>
  import PageHeader from './PageHeader.svelte';
  import { getServer, getServerPowerState, powerOnServer, powerOffServer, powerResetServer, testServerCapabilities, getPlugins, getLocations, getBootTask, createBootTask, cancelBootTask, listISOs, listTempOS, listScripts } from '../lib/api.js';
  import { onMount } from 'svelte';

  export let serverId;
  export let onBack;

  let server = null;
  let plugins = [];
  let locations = [];
  let loading = true;
  let error = null;
  let powerState = null;
  let powerActionsInProgress = {};
  let testingCapabilities = false;
  let expandedLogs = false;
  let bootTask = null;
  let isos = [];
  let loadingISOs = false;
  let selectedISO = null;
  let tempOSes = [];
  let loadingTempOSes = false;
  let selectedTempOS = null;
  let scripts = [];
  let loadingScripts = false;
  let selectedScript = null;
  let creatingBootTask = false;
  let refreshing = false;

  onMount(async () => {
    await loadAllData();
  });

  async function loadAllData() {
    await Promise.all([loadServer(), loadPlugins(), loadLocations(), loadISOs(), loadTempOSes(), loadScripts()]);
    if (server && serverSupportsPowerControl(server)) {
      await loadPowerState();
    }
    if (server) {
      await loadBootTask();
    }
  }

  async function handleRefresh() {
    refreshing = true;
    try {
      await loadAllData();
    } catch (err) {
      console.error('Failed to refresh:', err);
    } finally {
      refreshing = false;
    }
  }

  async function loadServer() {
    try {
      loading = true;
      error = null;
      server = await getServer(serverId);
      if (!server) {
        error = 'Server not found';
      }
    } catch (err) {
      error = err.message;
      console.error('Failed to load server:', err);
    } finally {
      loading = false;
    }
  }

  async function loadPlugins() {
    try {
      plugins = await getPlugins();
    } catch (err) {
      console.error('Failed to load plugins:', err);
    }
  }

  async function loadLocations() {
    try {
      locations = await getLocations();
    } catch (err) {
      console.error('Failed to load locations:', err);
    }
  }

  function serverSupportsPowerControl(server) {
    return server.plugin_categories && server.plugin_categories.includes('power_control');
  }

  async function loadPowerState() {
    if (!server || !serverSupportsPowerControl(server)) return;
    try {
      const result = await getServerPowerState(server.id);
      powerState = result.power_state;
    } catch (err) {
      console.error('Failed to get power state:', err);
      powerState = 'unknown';
    }
  }

  async function handlePowerOn() {
    if (!confirm(`Power on server "${server.name}"?`)) {
      return;
    }
    try {
      powerActionsInProgress = { ...powerActionsInProgress, power_on: true };
      await powerOnServer(server.id);
      setTimeout(() => loadPowerState(), 2000);
    } catch (err) {
      alert('Failed to power on server: ' + err.message);
    } finally {
      powerActionsInProgress = { ...powerActionsInProgress, power_on: false };
    }
  }

  async function handlePowerOff() {
    if (!confirm(`Power off server "${server.name}"?`)) {
      return;
    }
    try {
      powerActionsInProgress = { ...powerActionsInProgress, power_off: true };
      await powerOffServer(server.id);
      setTimeout(() => loadPowerState(), 2000);
    } catch (err) {
      alert('Failed to power off server: ' + err.message);
    } finally {
      powerActionsInProgress = { ...powerActionsInProgress, power_off: false };
    }
  }

  async function handlePowerReset() {
    if (!confirm(`Reset/reboot server "${server.name}"?`)) {
      return;
    }
    try {
      powerActionsInProgress = { ...powerActionsInProgress, power_reset: true };
      await powerResetServer(server.id);
      setTimeout(() => loadPowerState(), 2000);
    } catch (err) {
      alert('Failed to reset server: ' + err.message);
    } finally {
      powerActionsInProgress = { ...powerActionsInProgress, power_reset: false };
    }
  }

  async function testCapabilities() {
    if (!confirm(`Test capabilities for server "${server.name}"? This will test all available plugin capabilities.`)) {
      return;
    }
    testingCapabilities = true;
    try {
      await testServerCapabilities(server.id);
      await loadServer(); // Reload to get updated test results
      alert('Capability test completed!');
    } catch (err) {
      alert('Failed to test capabilities: ' + err.message);
    } finally {
      testingCapabilities = false;
    }
  }

  function getPluginName() {
    return plugins.find(p => p.id === server.plugin_id)?.name || 'N/A';
  }

  function getLocationName() {
    return locations.find(l => l.id === server.location_id)?.name || 'N/A';
  }

  async function loadISOs() {
    try {
      loadingISOs = true;
      isos = await listISOs();
    } catch (err) {
      console.error('Failed to load ISOs:', err);
      isos = [];
    } finally {
      loadingISOs = false;
    }
  }

  async function loadBootTask() {
    try {
      bootTask = await getBootTask(serverId);
    } catch (err) {
      console.error('Failed to load boot task:', err);
      bootTask = null;
    }
  }

  async function handleBootISO() {
    if (!selectedISO) {
      alert('Please select an ISO file');
      return;
    }

    if (!confirm(`Boot server "${server.name}" from ISO "${selectedISO.filename}"?`)) {
      return;
    }

    creatingBootTask = true;
    try {
      await createBootTask(serverId, {
        boot_type: 'iso',
        iso_url: selectedISO.url,
        description: `Boot from ${selectedISO.filename}`
      });
      await loadBootTask();
      alert('Boot task created successfully. Server will boot from ISO on next reboot.');
      selectedISO = null;
    } catch (err) {
      alert('Failed to create boot task: ' + err.message);
    } finally {
      creatingBootTask = false;
    }
  }

  async function loadTempOSes() {
    try {
      loadingTempOSes = true;
      tempOSes = await listTempOS();
    } catch (err) {
      console.error('Failed to load temporary OSes:', err);
      tempOSes = [];
    } finally {
      loadingTempOSes = false;
    }
  }

  async function handleBootTempOS() {
    if (!selectedTempOS) {
      alert('Please select a temporary OS');
      return;
    }

    const os = tempOSes.find(o => o.id === selectedTempOS);
    if (!os) {
      alert('Selected temporary OS not found');
      return;
    }

    if (!confirm(`Boot server "${server.name}" into ${os.name}?`)) {
      return;
    }

    creatingBootTask = true;
    try {
      await createBootTask(serverId, {
        boot_type: 'temp_os',
        temp_os_id: selectedTempOS,
        description: `Boot into ${os.name} temporary OS`
      });
      await loadBootTask();
      alert(`Boot task created successfully. Server will boot into ${os.name} on next reboot.`);
      selectedTempOS = null;
    } catch (err) {
      alert('Failed to create boot task: ' + err.message);
    } finally {
      creatingBootTask = false;
    }
  }

  async function loadScripts() {
    try {
      loadingScripts = true;
      scripts = await listScripts();
    } catch (err) {
      console.error('Failed to load scripts:', err);
      scripts = [];
    } finally {
      loadingScripts = false;
    }
  }

  async function handleRunScript() {
    if (!selectedScript) {
      alert('Please select a script');
      return;
    }

    const script = scripts.find(s => s.filename === selectedScript);
    if (!script) {
      alert('Selected script not found');
      return;
    }

    if (!confirm(`Boot server "${server.name}" into Alpine and run script "${script.filename}"?`)) {
      return;
    }

    creatingBootTask = true;
    try {
      await createBootTask(serverId, {
        boot_type: 'temp_os',
        temp_os_id: 'alpine-script',
        custom_script: script.filename,
        description: `Run script: ${script.filename}`
      });
      await loadBootTask();
      alert(`Boot task created successfully. Server will boot into Alpine and run "${script.filename}" on next reboot.`);
      selectedScript = null;
    } catch (err) {
      alert('Failed to create boot task: ' + err.message);
    } finally {
      creatingBootTask = false;
    }
  }

  async function handleCancelBootTask() {
    if (!confirm(`Cancel boot task for server "${server.name}"?`)) {
      return;
    }

    try {
      await cancelBootTask(serverId);
      await loadBootTask();
      alert('Boot task cancelled successfully.');
    } catch (err) {
      alert('Failed to cancel boot task: ' + err.message);
    }
  }

  function formatFileSize(mb) {
    if (mb < 1024) {
      return `${mb.toFixed(2)} MB`;
    } else {
      return `${(mb / 1024).toFixed(2)} GB`;
    }
  }
</script>

<div class="server-detail-page">
  <PageHeader title="Server Details" onNavigate={onBack}>
    <svelte:fragment slot="actions">
      <button class="refresh-button" on:click={handleRefresh} disabled={refreshing || loading}>
        <svg xmlns="http://www.w3.org/2000/svg" class="refresh-icon" class:spinning={refreshing} fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
        </svg>
        <span>{refreshing ? 'Refreshing...' : 'Refresh'}</span>
      </button>
    </svelte:fragment>
  </PageHeader>

  {#if loading}
  <div class="content-body">
    <div class="loading">Loading server details...</div>
  </div>
{:else if error}
  <div class="content-body">
    <div class="error">Error: {error}</div>
    <button class="btn-primary" on:click={onBack}>Back to Servers</button>
  </div>
{:else if server}
  <div class="content-body">
    <!-- Server Info Card -->
    <div class="card">
      <div class="card-header">
        <h2>{server.name}</h2>
        <div class="card-actions">
          <button class="btn-secondary" on:click={onBack}>Back to List</button>
        </div>
      </div>
      <div class="card-body">
        <div class="info-grid">
          <div class="info-item">
            <label>Server IP</label>
            <span>{server.server_ip}</span>
          </div>
          <div class="info-item">
            <label>Description</label>
            <span>{server.description || 'N/A'}</span>
          </div>
          <div class="info-item">
            <label>CPU</label>
            <span>{server.cpu_count}x {server.cpu_model || 'N/A'}</span>
          </div>
          <div class="info-item">
            <label>RAM</label>
            <span>{server.ram_gb ? server.ram_gb + ' GB' : 'N/A'}</span>
          </div>
          <div class="info-item">
            <label>Location</label>
            <span>{getLocationName()}</span>
          </div>
          <div class="info-item">
            <label>Plugin</label>
            <span>{getPluginName()}</span>
          </div>
          <div class="info-item">
            <label>Status</label>
            <span class="status-badge" class:enabled={server.enabled} class:disabled={!server.enabled}>
              {server.enabled ? 'Enabled' : 'Disabled'}
            </span>
          </div>
          <div class="info-item">
            <label>Boot Mode</label>
            <span>{server.boot_mode ? server.boot_mode.toUpperCase() : 'N/A'}</span>
          </div>
        </div>
      </div>
    </div>

    <!-- Power Control Card -->
    {#if serverSupportsPowerControl(server)}
      <div class="card">
        <div class="card-header">
          <h3>Power Control</h3>
        </div>
        <div class="card-body">
          <div class="power-control-section">
            <div class="power-state-display">
              <label>Current Power State:</label>
              <span class="power-state-badge" class:power-on={powerState === 'on'} class:power-off={powerState === 'off'} class:power-unknown={powerState === 'unknown' || !powerState}>
                {powerState || 'Loading...'}
              </span>
              <button class="btn-icon-only btn-small" on:click={loadPowerState} title="Refresh power state">
                <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                </svg>
              </button>
            </div>
            <div class="power-control-buttons">
              <button 
                class="btn-power btn-power-on" 
                on:click={handlePowerOn} 
                disabled={powerActionsInProgress.power_on}
                title="Power On"
              >
                {#if powerActionsInProgress.power_on}
                  <svg xmlns="http://www.w3.org/2000/svg" class="btn-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                  </svg>
                {:else}
                  <svg xmlns="http://www.w3.org/2000/svg" class="btn-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
                  </svg>
                {/if}
                Power On
              </button>
              <button 
                class="btn-power btn-power-off" 
                on:click={handlePowerOff} 
                disabled={powerActionsInProgress.power_off}
                title="Power Off"
              >
                {#if powerActionsInProgress.power_off}
                  <svg xmlns="http://www.w3.org/2000/svg" class="btn-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                  </svg>
                {:else}
                  <svg xmlns="http://www.w3.org/2000/svg" class="btn-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
                  </svg>
                {/if}
                Power Off
              </button>
              <button 
                class="btn-power btn-power-reset" 
                on:click={handlePowerReset} 
                disabled={powerActionsInProgress.power_reset}
                title="Reset/Reboot"
              >
                {#if powerActionsInProgress.power_reset}
                  <svg xmlns="http://www.w3.org/2000/svg" class="btn-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                  </svg>
                {:else}
                  <svg xmlns="http://www.w3.org/2000/svg" class="btn-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                  </svg>
                {/if}
                Reset/Reboot
              </button>
            </div>
          </div>
        </div>
      </div>
    {/if}

    <!-- Boot Operations Card -->
    <div class="card">
      <div class="card-header">
        <h3>Boot Operations</h3>
      </div>
      <div class="card-body">
        {#if bootTask}
          <div class="boot-task-info">
            <div class="boot-task-status">
              <strong>Active Boot Task:</strong>
              <span class="status-badge" class:pending={bootTask.status === 'pending'} class:in-progress={bootTask.status === 'in_progress'} class:completed={bootTask.status === 'completed'} class:failed={bootTask.status === 'failed'}>
                {bootTask.status.replace('_', ' ')}
              </span>
            </div>
            <div class="boot-task-details">
              <div><strong>Type:</strong> {bootTask.boot_type.replace('_', ' ')}</div>
              {#if bootTask.description}
                <div><strong>Description:</strong> {bootTask.description}</div>
              {/if}
              {#if bootTask.iso_url}
                <div><strong>ISO:</strong> {bootTask.iso_url.split('/').pop()}</div>
              {/if}
              {#if bootTask.created_at}
                <div><strong>Created:</strong> {new Date(bootTask.created_at).toLocaleString()}</div>
              {/if}
            </div>
            {#if bootTask.status === 'pending' || bootTask.status === 'in_progress'}
              <button class="btn-secondary" on:click={handleCancelBootTask}>
                Cancel Boot Task
              </button>
            {/if}
          </div>
        {:else}
          <div class="boot-options-section">
            <div class="boot-option-group">
              <h4>Boot from ISO</h4>
              {#if loadingISOs}
                <p>Loading ISOs...</p>
              {:else if isos.length === 0}
                <p class="no-isos">No ISO files found. Place ISO files in the <code>isos/</code> directory on the server.</p>
              {:else}
                <div class="iso-selector">
                  <label for="iso-select">Select ISO:</label>
                  <select id="iso-select" bind:value={selectedISO}>
                    <option value={null}>-- Select an ISO --</option>
                    {#each isos as iso}
                      <option value={iso}>{iso.filename} ({formatFileSize(iso.size_mb)})</option>
                    {/each}
                  </select>
                  {#if selectedISO}
                    <div class="iso-info">
                      <small>Size: {formatFileSize(selectedISO.size_mb)}</small>
                    </div>
                  {/if}
                  <button class="btn-primary" on:click={handleBootISO} disabled={!selectedISO || creatingBootTask}>
                    {creatingBootTask ? 'Creating...' : 'Boot from ISO'}
                  </button>
                </div>
              {/if}
            </div>

            {#if tempOSes.length > 0}
              <div class="boot-option-group">
                <h4>Boot Temporary OS</h4>
                <p class="boot-option-description">Boot into a temporary OS for manual operations.</p>
                <div class="form-group">
                  <label for="temp-os-select">Select Temporary OS:</label>
                  <select id="temp-os-select" bind:value={selectedTempOS} disabled={creatingBootTask || loadingTempOSes}>
                    <option value="">-- Select OS --</option>
                    {#each tempOSes as os}
                      <option value={os.id}>{os.name}{os.version ? ` (${os.version})` : ''}</option>
                    {/each}
                  </select>
                </div>
                {#if selectedTempOS}
                  {@const os = tempOSes.find(o => o.id === selectedTempOS)}
                  {#if os}
                    <p class="boot-option-description" style="font-size: 0.9em; color: #666; margin-top: 0.5em;">
                      {os.description || 'No description available'}
                    </p>
                  {/if}
                {/if}
                <button class="btn-primary" on:click={handleBootTempOS} disabled={creatingBootTask || !selectedTempOS || loadingTempOSes}>
                  {creatingBootTask ? 'Creating...' : 'Boot Temporary OS'}
                </button>
              </div>
            {/if}
          </div>
        {/if}
      </div>
    </div>

    <!-- Capabilities Card -->
    <div class="card">
      <div class="card-header">
        <h3>Capabilities</h3>
        <div class="card-actions">
          <button class="btn-secondary btn-small" on:click={testCapabilities} disabled={testingCapabilities}>
            {testingCapabilities ? 'Testing...' : 'Test Capabilities'}
          </button>
        </div>
      </div>
      <div class="card-body">
        {#if server.tested_capabilities && server.tested_capabilities.length > 0}
          <div class="capabilities-list">
            {#each server.tested_capabilities as capability}
              <div class="capability-item">
                <span class="capability-badge tested">
                  {capability.replace(/_/g, ' ')}
                  <svg xmlns="http://www.w3.org/2000/svg" class="capability-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
                  </svg>
                </span>
              </div>
            {/each}
          </div>
          {#if server.test_logs}
            <div class="test-logs-section">
              <button class="btn-text" on:click={() => expandedLogs = !expandedLogs}>
                {expandedLogs ? 'Hide' : 'Show'} Test Logs
              </button>
              {#if expandedLogs}
                <pre class="test-logs-content">{server.test_logs}</pre>
              {/if}
            </div>
          {/if}
        {:else}
          <div class="no-capabilities">
            <p>No capabilities tested yet. Click "Test Capabilities" to test available plugin capabilities.</p>
          </div>
        {/if}
      </div>
    </div>

    <!-- Disks Card -->
    {#if server.disks && server.disks.length > 0}
      <div class="card">
        <div class="card-header">
          <h3>Disks</h3>
        </div>
        <div class="card-body">
          <table class="data-table">
            <thead>
              <tr>
                <th>Type</th>
                <th>Capacity</th>
                <th>Description</th>
              </tr>
            </thead>
            <tbody>
              {#each server.disks as disk}
                <tr>
                  <td>{disk.type}</td>
                  <td>{disk.capacity_gb} GB</td>
                  <td>{disk.description || 'N/A'}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      </div>
    {/if}

    <!-- Network Ports Card -->
    {#if server.network_ports && server.network_ports.length > 0}
      <div class="card">
        <div class="card-header">
          <h3>Network Ports</h3>
        </div>
        <div class="card-body">
          <table class="data-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>MAC Address</th>
                <th>Speed</th>
                <th>LAG Group</th>
                <th>Bandwidth Monitor</th>
                <th>PXE Boot</th>
                <th>PXE IP</th>
              </tr>
            </thead>
            <tbody>
              {#each server.network_ports as port}
                <tr>
                  <td>{port.name}</td>
                  <td>{port.mac_address || 'N/A'}</td>
                  <td>{port.speed_mbps ? port.speed_mbps + ' Mbps' : 'N/A'}</td>
                  <td>{port.lag_group || 'N/A'}</td>
                  <td>{port.bandwidth_monitor ? 'Yes' : 'No'}</td>
                  <td>{port.pxe_boot ? 'Yes' : 'No'}</td>
                  <td>{port.pxe_ip || 'N/A'}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      </div>
    {/if}
  </div>
{/if}
</div>

<style>
  .content-body {
    padding: 32px;
  }

  .loading, .error {
    padding: 32px;
    text-align: center;
    font-size: 16px;
  }

  .error {
    color: #ef4444;
  }

  .card {
    background: white;
    border-radius: 12px;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
    margin-bottom: 24px;
    overflow: hidden;
  }

  .card-header {
    padding: 20px 24px;
    border-bottom: 1px solid #e5e7eb;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }

  .card-header h2 {
    margin: 0;
    font-size: 24px;
    font-weight: 700;
    color: #111827;
  }

  .card-header h3 {
    margin: 0;
    font-size: 18px;
    font-weight: 600;
    color: #111827;
  }


  .refresh-button {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 16px;
    background: white;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    color: var(--text-primary);
    font-size: 14px;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s ease;
    white-space: nowrap;
  }

  .refresh-button:hover:not(:disabled) {
    background: #f8fafc;
    border-color: var(--primary-color);
    color: var(--primary-color);
  }

  .refresh-button:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .refresh-icon {
    width: 18px;
    height: 18px;
    transition: transform 0.3s ease;
  }

  .refresh-icon.spinning {
    animation: spin 1s linear infinite;
  }

  @keyframes spin {
    from {
      transform: rotate(0deg);
    }
    to {
      transform: rotate(360deg);
    }
  }

  .card-actions {
    display: flex;
    gap: 8px;
  }

  .card-body {
    padding: 24px;
  }

  .info-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
    gap: 20px;
  }

  .info-item {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .info-item label {
    font-size: 12px;
    font-weight: 600;
    color: #6b7280;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }

  .info-item span {
    font-size: 16px;
    color: #111827;
  }

  .status-badge {
    display: inline-block;
    padding: 4px 12px;
    border-radius: 12px;
    font-size: 12px;
    font-weight: 600;
    text-transform: uppercase;
  }

  .status-badge.enabled {
    background: #dcfce7;
    color: #166534;
  }

  .status-badge.disabled {
    background: #fee2e2;
    color: #991b1b;
  }

  .power-control-section {
    display: flex;
    flex-direction: column;
    gap: 20px;
  }

  .power-state-display {
    display: flex;
    align-items: center;
    gap: 12px;
  }

  .power-state-display label {
    font-weight: 600;
    color: #374151;
  }

  .power-state-badge {
    padding: 6px 16px;
    border-radius: 12px;
    font-size: 14px;
    font-weight: 600;
    text-transform: uppercase;
  }

  .power-state-badge.power-on {
    background: #dcfce7;
    color: #166534;
  }

  .power-state-badge.power-off {
    background: #fee2e2;
    color: #991b1b;
  }

  .power-state-badge.power-unknown {
    background: #f3f4f6;
    color: #6b7280;
  }

  .power-control-buttons {
    display: flex;
    gap: 12px;
    flex-wrap: wrap;
  }

  .btn-power {
    padding: 10px 20px;
    border: none;
    border-radius: 8px;
    cursor: pointer;
    transition: all 0.2s ease;
    display: flex;
    align-items: center;
    gap: 8px;
    font-weight: 600;
    font-size: 14px;
  }

  .btn-power:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .btn-power-on {
    background: #22c55e;
    color: white;
  }

  .btn-power-on:hover:not(:disabled) {
    background: #16a34a;
  }

  .btn-power-off {
    background: #ef4444;
    color: white;
  }

  .btn-power-off:hover:not(:disabled) {
    background: #dc2626;
  }

  .btn-power-reset {
    background: #f59e0b;
    color: white;
  }

  .btn-power-reset:hover:not(:disabled) {
    background: #d97706;
  }

  .btn-icon {
    width: 18px;
    height: 18px;
  }

  .btn-icon-only {
    background: none;
    border: none;
    padding: 6px;
    cursor: pointer;
    border-radius: 6px;
    transition: background 0.2s ease;
  }

  .btn-icon-only:hover {
    background: #f3f4f6;
  }

  .btn-small {
    padding: 6px 12px;
    font-size: 12px;
  }

  .capabilities-list {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }

  .capability-item {
    display: flex;
  }

  .capability-badge {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 6px 12px;
    border-radius: 8px;
    font-size: 13px;
    font-weight: 600;
  }

  .capability-badge.tested {
    background: #dcfce7;
    color: #166534;
  }

  .capability-icon {
    width: 14px;
    height: 14px;
  }

  .test-logs-section {
    margin-top: 20px;
    padding-top: 20px;
    border-top: 1px solid #e5e7eb;
  }

  .test-logs-content {
    margin-top: 12px;
    padding: 16px;
    background: #f9fafb;
    border-radius: 8px;
    font-size: 12px;
    font-family: 'Courier New', monospace;
    white-space: pre-wrap;
    word-wrap: break-word;
    max-height: 400px;
    overflow-y: auto;
  }

  .no-capabilities {
    padding: 20px;
    text-align: center;
    color: #6b7280;
  }

  .data-table {
    width: 100%;
    border-collapse: collapse;
  }

  .data-table th {
    text-align: left;
    padding: 12px;
    font-weight: 600;
    color: #374151;
    border-bottom: 2px solid #e5e7eb;
  }

  .data-table td {
    padding: 12px;
    border-bottom: 1px solid #e5e7eb;
    color: #111827;
  }

  .data-table tr:hover {
    background: #f9fafb;
  }

  .btn-primary {
    padding: 10px 20px;
    background: #3b82f6;
    color: white;
    border: none;
    border-radius: 8px;
    cursor: pointer;
    font-weight: 600;
    transition: background 0.2s ease;
  }

  .btn-primary:hover {
    background: #2563eb;
  }

  .btn-secondary {
    padding: 10px 20px;
    background: #f3f4f6;
    color: #374151;
    border: none;
    border-radius: 8px;
    cursor: pointer;
    font-weight: 600;
    transition: background 0.2s ease;
  }

  .btn-secondary:hover {
    background: #e5e7eb;
  }

  .btn-text {
    background: none;
    border: none;
    color: #3b82f6;
    cursor: pointer;
    font-weight: 600;
    padding: 4px 8px;
    border-radius: 4px;
    transition: background 0.2s ease;
  }

  .btn-text:hover {
    background: #eff6ff;
  }

  .boot-task-info {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .boot-task-status {
    display: flex;
    align-items: center;
    gap: 12px;
  }

  .boot-task-details {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 12px;
    background: #f9fafb;
    border-radius: 8px;
    font-size: 14px;
  }

  .boot-task-details div {
    display: flex;
    gap: 8px;
  }

  .boot-task-details strong {
    min-width: 100px;
    color: #374151;
  }

  .status-badge {
    padding: 4px 12px;
    border-radius: 12px;
    font-size: 12px;
    font-weight: 600;
    text-transform: capitalize;
  }

  .status-badge.pending {
    background: #fef3c7;
    color: #92400e;
  }

  .status-badge.in-progress {
    background: #dbeafe;
    color: #1e40af;
  }

  .status-badge.completed {
    background: #dcfce7;
    color: #166534;
  }

  .status-badge.failed {
    background: #fee2e2;
    color: #991b1b;
  }

  .boot-options-section {
    display: flex;
    flex-direction: column;
    gap: 24px;
  }

  .boot-option-group {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .boot-option-group h4 {
    margin: 0;
    font-size: 16px;
    font-weight: 600;
    color: #111827;
  }

  .boot-option-description {
    color: #6b7280;
    font-size: 14px;
    margin: 0;
  }

  .no-isos {
    color: #6b7280;
    font-size: 14px;
  }

  .iso-selector {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .iso-selector label {
    font-weight: 600;
    color: #374151;
    font-size: 14px;
  }

  .iso-selector select {
    padding: 10px 12px;
    border: 1px solid #d1d5db;
    border-radius: 8px;
    font-size: 14px;
    font-family: inherit;
    background: white;
    cursor: pointer;
    transition: border-color 0.2s ease;
  }

  .iso-selector select:focus {
    outline: none;
    border-color: #3b82f6;
    box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.1);
  }

  .iso-info {
    color: #6b7280;
    font-size: 13px;
  }
  .boot-task-info {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .boot-task-status {
    display: flex;
    align-items: center;
    gap: 12px;
  }

  .boot-task-details {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 12px;
    background: #f9fafb;
    border-radius: 8px;
    font-size: 14px;
  }

  .boot-task-details div {
    display: flex;
    gap: 8px;
  }

  .boot-task-details strong {
    min-width: 100px;
    color: #374151;
  }

  .status-badge {
    padding: 4px 12px;
    border-radius: 12px;
    font-size: 12px;
    font-weight: 600;
    text-transform: capitalize;
  }

  .status-badge.pending {
    background: #fef3c7;
    color: #92400e;
  }

  .status-badge.in-progress {
    background: #dbeafe;
    color: #1e40af;
  }

  .status-badge.completed {
    background: #dcfce7;
    color: #166534;
  }

  .status-badge.failed {
    background: #fee2e2;
    color: #991b1b;
  }

  .no-isos {
    color: #6b7280;
    font-size: 14px;
  }

  .iso-selector {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .iso-selector label {
    font-weight: 600;
    color: #374151;
    font-size: 14px;
  }

  .iso-selector select {
    padding: 10px 12px;
    border: 1px solid #d1d5db;
    border-radius: 8px;
    font-size: 14px;
    font-family: inherit;
    background: white;
    cursor: pointer;
    transition: border-color 0.2s ease;
  }

  .iso-selector select:focus {
    outline: none;
    border-color: #3b82f6;
    box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.1);
  }

  .iso-info {
    color: #6b7280;
    font-size: 13px;
  }
</style>
