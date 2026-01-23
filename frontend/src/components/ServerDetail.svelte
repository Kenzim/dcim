<script>
  import PageHeader from './PageHeader.svelte';
  import { getServer, getServerPowerState, powerOnServer, powerOffServer, powerResetServer, testServerCapabilities, getPlugins, getLocations, getBootTask, createBootTask, cancelBootTask, listISOs, listTempOS, getScripts, getOSTemplates, getInstallationHistory, generatePassword } from '../lib/api.js';
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
  let templates = [];
  let loadingTemplates = false;
  let selectedTemplate = null;
  let templateParameters = {};
  let bootOperationsExpanded = true;
  let hardwareExpanded = false;
  let installationHistory = [];
  let loadingInstallationHistory = false;
  let installationHistoryExpanded = false;

  onMount(async () => {
    await loadAllData();
  });

  async function loadAllData() {
    await Promise.all([loadServer(), loadPlugins(), loadLocations(), loadISOs(), loadTempOSes(), loadScripts(), loadTemplates()]);
    if (server && serverSupportsPowerControl(server)) {
      await loadPowerState();
    }
    if (server) {
      await Promise.all([loadBootTask(), loadInstallationHistory()]);
    }
  }

  async function loadInstallationHistory() {
    try {
      loadingInstallationHistory = true;
      installationHistory = await getInstallationHistory(serverId) || [];
    } catch (err) {
      // Silently handle errors - installation history is optional
      installationHistory = [];
    } finally {
      loadingInstallationHistory = false;
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
      // Filter to only enabled scripts that are user-executable or all enabled scripts for admin
      const allScripts = await getScripts();
      scripts = allScripts.filter(s => s.enabled);
    } catch (err) {
      console.error('Failed to load scripts:', err);
      scripts = [];
    } finally {
      loadingScripts = false;
    }
  }

  async function loadTemplates() {
    try {
      loadingTemplates = true;
      templates = await getOSTemplates();
    } catch (err) {
      console.error('Failed to load templates:', err);
      templates = [];
    } finally {
      loadingTemplates = false;
    }
  }

  function handleTemplateChange() {
    if (!selectedTemplate) {
      templateParameters = {};
      return;
    }
    
    const template = templates.find(t => t.id === selectedTemplate);
    if (template) {
      // Initialize parameters with defaults
      templateParameters = {};
      for (const [paramName, param] of Object.entries(template.parameters)) {
        if (param.default !== null && param.default !== undefined) {
          templateParameters[paramName] = param.default;
        } else if (param.type === 'boolean') {
          templateParameters[paramName] = false;
        } else if (param.type === 'number') {
          templateParameters[paramName] = 0;
        } else {
          templateParameters[paramName] = '';
        }
      }
    }
  }

  async function handleStartInstallation() {
    if (!selectedTemplate) {
      alert('Please select an OS template');
      return;
    }

    const template = templates.find(t => t.id === selectedTemplate);
    if (!template) {
      alert('Selected template not found');
      return;
    }

    // Validate required parameters
    for (const [paramName, param] of Object.entries(template.parameters)) {
      if (param.required) {
        const value = templateParameters[paramName];
        if (value === null || value === undefined || value === '') {
          alert(`Parameter "${param.label}" is required`);
          return;
        }
      }
    }

    if (!confirm(`Start OS installation for server "${server.name}" using template "${template.name}"?`)) {
      return;
    }

    creatingBootTask = true;
    try {
      await createBootTask(serverId, {
        boot_type: 'linux_script',
        template_id: selectedTemplate,
        template_parameters: templateParameters,
        description: `Install ${template.name}`
      });
      await loadBootTask();
      alert(`Installation job created successfully. Server will boot and start installation on next reboot.`);
      selectedTemplate = null;
      templateParameters = {};
    } catch (err) {
      alert('Failed to create installation job: ' + err.message);
    } finally {
      creatingBootTask = false;
    }
  }

  async function handleRunScript() {
    if (!selectedScript) {
      alert('Please select a script');
      return;
    }

    // selectedScript is the script name or ID
    const script = scripts.find(s => s.name === selectedScript || s.id.toString() === selectedScript);
    if (!script) {
      alert('Selected script not found');
      return;
    }

    if (!confirm(`Boot server "${server.name}" into Ubuntu Live OS and run script "${script.name}"?`)) {
      return;
    }

    creatingBootTask = true;
    try {
      // Use script name (backend accepts name or ID)
      await createBootTask(serverId, {
        boot_type: 'temp_os',
        temp_os_id: 'debian-live',
        custom_script: script.name,
        description: `Run script: ${script.name}`
      });
      await loadBootTask();
      alert(`Boot task created successfully. Server will boot into Ubuntu Live OS and run "${script.name}" on next reboot.`);
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

  async function handleGeneratePassword(paramName) {
    const template = templates.find(t => t.id === selectedTemplate);
    if (!template || !template.parameters[paramName]) {
      return;
    }
    
    const param = template.parameters[paramName];
    const generateConfig = param.generate || {};
    
    // Use template config if available, otherwise use defaults
    const length = generateConfig.length || 16;
    const charset = generateConfig.charset || 'alphanumeric';
    const excludeAmbiguous = generateConfig.exclude_ambiguous !== undefined 
      ? generateConfig.exclude_ambiguous 
      : true;
    
    try {
      const password = await generatePassword(length, charset, excludeAmbiguous);
      templateParameters[paramName] = password;
      // Trigger reactivity
      templateParameters = { ...templateParameters };
    } catch (err) {
      alert('Failed to generate password: ' + err.message);
    }
  }

  async function handleCopyPassword(paramName, event) {
    const password = templateParameters[paramName];
    if (!password) return;
    
    try {
      await navigator.clipboard.writeText(password);
      // Show temporary feedback
      const button = event?.target?.closest('button');
      if (button) {
        const originalTitle = button.title;
        button.title = 'Copied!';
        setTimeout(() => {
          button.title = originalTitle;
        }, 2000);
      }
    } catch (err) {
      // Fallback for older browsers
      const textArea = document.createElement('textarea');
      textArea.value = password;
      textArea.style.position = 'fixed';
      textArea.style.left = '-999999px';
      document.body.appendChild(textArea);
      textArea.select();
      try {
        document.execCommand('copy');
        const button = event?.target?.closest('button');
        if (button) {
          const originalTitle = button.title;
          button.title = 'Copied!';
          setTimeout(() => {
            button.title = originalTitle;
          }, 2000);
        }
      } catch (e) {
        alert('Failed to copy password. Please copy manually.');
      }
      document.body.removeChild(textArea);
    }
  }

  async function handleCopyCredential(value, event) {
    if (!value) return;
    
    try {
      await navigator.clipboard.writeText(String(value));
      // Show temporary feedback
      const button = event?.target?.closest('button');
      if (button) {
        const originalTitle = button.title;
        button.title = 'Copied!';
        setTimeout(() => {
          button.title = originalTitle;
        }, 2000);
      }
    } catch (err) {
      // Fallback for older browsers
      const textArea = document.createElement('textarea');
      textArea.value = String(value);
      textArea.style.position = 'fixed';
      textArea.style.left = '-999999px';
      document.body.appendChild(textArea);
      textArea.select();
      try {
        document.execCommand('copy');
        const button = event?.target?.closest('button');
        if (button) {
          const originalTitle = button.title;
          button.title = 'Copied!';
          setTimeout(() => {
            button.title = originalTitle;
          }, 2000);
        }
      } catch (e) {
        alert('Failed to copy. Please copy manually.');
      }
      document.body.removeChild(textArea);
    }
  }
</script>

<div class="server-detail-page">
  <PageHeader title="Server Details">
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
    <!-- Top Section: Server Info and Power Control Side by Side -->
    <div class="top-section">
      <!-- Server Info Card -->
      <div class="card">
        <div class="card-header">
          <h2>{server.name}</h2>
          <div class="card-actions">
            <button class="btn-secondary btn-small" on:click={onBack}>Back</button>
          </div>
        </div>
        <div class="card-body">
          <div class="info-grid">
            <div class="info-item">
              <label>Server IP</label>
              <span>{server.server_ip}</span>
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
              <label>PXE Boot</label>
              <span>{server.pxe_boot_mode ? server.pxe_boot_mode.toUpperCase() : (server.boot_mode ? server.boot_mode.toUpperCase() : 'N/A')}</span>
            </div>
            <div class="info-item">
              <label>OS Boot</label>
              <span>{server.os_boot_mode ? server.os_boot_mode.toUpperCase() : (server.boot_mode ? server.boot_mode.toUpperCase() : 'N/A')}</span>
            </div>
            {#if server.description}
              <div class="info-item info-item-full">
                <label>Description</label>
                <span>{server.description}</span>
              </div>
            {/if}
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
                <label>Power State:</label>
                <span class="power-state-badge" class:power-on={powerState === 'on'} class:power-off={powerState === 'off'} class:power-unknown={powerState === 'unknown' || !powerState}>
                  {powerState || 'Loading...'}
                </span>
                <button class="btn-icon-only btn-small" on:click={loadPowerState} title="Refresh">
                  <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" width="14" height="14">
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
                    <svg xmlns="http://www.w3.org/2000/svg" class="btn-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor" width="16" height="16">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                    </svg>
                  {:else}
                    <svg xmlns="http://www.w3.org/2000/svg" class="btn-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor" width="16" height="16">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
                    </svg>
                  {/if}
                  On
                </button>
                <button 
                  class="btn-power btn-power-off" 
                  on:click={handlePowerOff} 
                  disabled={powerActionsInProgress.power_off}
                  title="Power Off"
                >
                  {#if powerActionsInProgress.power_off}
                    <svg xmlns="http://www.w3.org/2000/svg" class="btn-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor" width="16" height="16">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                    </svg>
                  {:else}
                    <svg xmlns="http://www.w3.org/2000/svg" class="btn-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor" width="16" height="16">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
                    </svg>
                  {/if}
                  Off
                </button>
                <button 
                  class="btn-power btn-power-reset" 
                  on:click={handlePowerReset} 
                  disabled={powerActionsInProgress.power_reset}
                  title="Reset/Reboot"
                >
                  {#if powerActionsInProgress.power_reset}
                    <svg xmlns="http://www.w3.org/2000/svg" class="btn-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor" width="16" height="16">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                    </svg>
                  {:else}
                    <svg xmlns="http://www.w3.org/2000/svg" class="btn-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor" width="16" height="16">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                    </svg>
                  {/if}
                  Reset
                </button>
              </div>
            </div>
          </div>
        </div>
      {/if}
    </div>

    <!-- Boot Operations Card -->
    <div class="card">
      <div class="card-header collapsible" on:click={() => bootOperationsExpanded = !bootOperationsExpanded}>
        <h3>Boot Operations</h3>
        <svg xmlns="http://www.w3.org/2000/svg" class="collapse-icon" class:expanded={bootOperationsExpanded} fill="none" viewBox="0 0 24 24" stroke="currentColor" width="20" height="20">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
        </svg>
      </div>
      {#if bootOperationsExpanded}
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
              <div><strong>Type:</strong> <span>{bootTask.boot_type.replace('_', ' ')}</span></div>
              {#if bootTask.description}
                <div><strong>Description:</strong> <span>{bootTask.description}</span></div>
              {/if}
              {#if bootTask.iso_url}
                <div><strong>ISO:</strong> <span>{bootTask.iso_url.split('/').pop()}</span></div>
              {/if}
              {#if bootTask.created_at}
                <div><strong>Created:</strong> <span>{new Date(bootTask.created_at).toLocaleString()}</span></div>
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

            <div class="boot-option">
              <h4>Run Custom Script</h4>
              <p class="boot-option-description">Boot into Ubuntu Live OS (squashfs) and run a custom script from the database.</p>
              {#if loadingScripts}
                <p>Loading scripts...</p>
              {:else if scripts.length === 0}
                <p class="no-isos">No enabled scripts found. Create scripts in the <strong>Scripts</strong> section.</p>
              {:else}
                <div class="form-group">
                  <label for="script-select">Select Script:</label>
                  <select id="script-select" bind:value={selectedScript} disabled={creatingBootTask}>
                    <option value="">-- Select a script --</option>
                    {#each scripts as script}
                      <option value={script.name}>
                        {script.name}
                        {#if script.description}
                          - {script.description}
                        {/if}
                      </option>
                    {/each}
                  </select>
                </div>
                <button class="btn-primary" on:click={handleRunScript} disabled={creatingBootTask || !selectedScript}>
                  {creatingBootTask ? 'Creating...' : 'Run Script'}
                </button>
              {/if}
            </div>

            {#if templates.length > 0}
              <div class="boot-option-group">
                <h4>Install OS</h4>
                <p class="boot-option-description">Install an operating system using a pre-configured template.</p>
                {#if loadingTemplates}
                  <p>Loading templates...</p>
                {:else}
                  <div class="form-group">
                    <label for="template-select">Select OS Template:</label>
                    <select id="template-select" bind:value={selectedTemplate} on:change={handleTemplateChange} disabled={creatingBootTask || loadingTemplates}>
                      <option value="">-- Select a template --</option>
                      {#each templates as template}
                        <option value={template.id}>{template.name}</option>
                      {/each}
                    </select>
                  </div>
                  
                  {#if selectedTemplate}
                    {@const template = templates.find(t => t.id === selectedTemplate)}
                    {#if template}
                      <div class="template-info">
                        <p class="template-description">{template.description}</p>
                        
                        {#if Object.keys(template.parameters).length > 0}
                          {@const requiredParams = Object.entries(template.parameters).filter(([_, p]) => p.required)}
                          {#if requiredParams.length > 0}
                            <div class="template-parameters-form">
                              {#each requiredParams as [paramName, param]}
                                <div class="form-group">
                                  <label for="param-{paramName}">
                                    {param.label}
                                    <span class="required">*</span>
                                  </label>
                                  
                                  {#if param.type === 'select' && param.options}
                                    <select id="param-{paramName}" bind:value={templateParameters[paramName]} disabled={creatingBootTask}>
                                      <option value="">-- Select --</option>
                                      {#each param.options as option}
                                        <option value={option}>{option}</option>
                                      {/each}
                                    </select>
                                  {:else if param.type === 'password'}
                                    {@const generateConfig = param.generate || {}}
                                    {@const showGenerate = generateConfig.enabled !== false}
                                    <div style="display: flex; gap: 8px; align-items: center;">
                                      <input id="param-{paramName}" type="text" bind:value={templateParameters[paramName]} placeholder={param.help || ''} disabled={creatingBootTask} style="flex: 1;" />
                                      {#if showGenerate}
                                        <button type="button" class="btn-secondary btn-small" on:click={() => handleGeneratePassword(paramName)} disabled={creatingBootTask} title="Generate password">
                                          <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" style="width: 16px; height: 16px;">
                                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                                          </svg>
                                        </button>
                                      {/if}
                                      <button type="button" class="btn-secondary btn-small" on:click={(e) => handleCopyPassword(paramName, e)} disabled={creatingBootTask || !templateParameters[paramName]} title="Copy password">
                                        <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" style="width: 16px; height: 16px;">
                                          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
                                        </svg>
                                      </button>
                                    </div>
                                  {:else if param.type === 'number'}
                                    <input id="param-{paramName}" type="number" bind:value={templateParameters[paramName]} placeholder={param.help || ''} disabled={creatingBootTask} />
                                  {:else if param.type === 'boolean'}
                                    <label class="checkbox-label">
                                      <input type="checkbox" bind:checked={templateParameters[paramName]} disabled={creatingBootTask} />
                                      <span>{param.help || 'Enable'}</span>
                                    </label>
                                  {:else}
                                    <input id="param-{paramName}" type="text" bind:value={templateParameters[paramName]} placeholder={param.help || ''} disabled={creatingBootTask} />
                                  {/if}
                                  
                                  {#if param.help && param.type !== 'boolean'}
                                    <small class="field-help">{param.help}</small>
                                  {/if}
                                </div>
                              {/each}
                            </div>
                          {/if}
                        {/if}
                      </div>
                    {/if}
                  {/if}
                  
                  <button class="btn-primary" on:click={handleStartInstallation} disabled={creatingBootTask || !selectedTemplate || loadingTemplates}>
                    {creatingBootTask ? 'Creating...' : 'Start Installation'}
                  </button>
                {/if}
              </div>
            {/if}
          </div>
        {/if}
      </div>
      {/if}
    </div>

    <!-- Hardware Section: Disks, Network Ports, Credentials, Capabilities -->
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
                <th>Serial Number</th>
                <th>OS Disk</th>
                <th>Description</th>
              </tr>
            </thead>
            <tbody>
              {#each server.disks as disk}
                <tr>
                  <td>{disk.type}</td>
                  <td>{disk.capacity_gb} GB</td>
                  <td>{disk.serial_number || 'N/A'}</td>
                  <td>{disk.is_os_disk ? '✓' : '—'}</td>
                  <td>{disk.description || 'N/A'}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      </div>
    {/if}

    <!-- Credentials Card -->
    {#if server.credentials && Object.keys(server.credentials).length > 0}
      <div class="card">
        <div class="card-header">
          <h3>Saved Credentials</h3>
        </div>
        <div class="card-body">
          <div class="credentials-info">
            {#if server.credentials.os_type}
              <div class="credential-item">
                <strong>OS Type:</strong> {server.credentials.os_type}
              </div>
            {/if}
            {#if server.credentials.template_id}
              <div class="credential-item">
                <strong>Template:</strong> {server.credentials.template_id}
              </div>
            {/if}
            {#if server.credentials.last_updated}
              <div class="credential-item">
                <strong>Last Updated:</strong> {new Date(server.credentials.last_updated).toLocaleString()}
              </div>
            {/if}
            {#each Object.entries(server.credentials) as [key, value]}
              {#if key !== 'os_type' && key !== 'template_id' && key !== 'last_updated' && value}
                <div class="credential-item">
                  <strong>{key.replace('_', ' ').replace(/\b\w/g, l => l.toUpperCase())}:</strong>
                  <div style="display: flex; gap: 8px; align-items: center; margin-top: 4px;">
                    <code style="flex: 1; padding: 6px 10px; background: #f3f4f6; border-radius: 4px; font-family: monospace;">{value}</code>
                    <button type="button" class="btn-secondary btn-small" on:click={(e) => handleCopyCredential(value, e)} title="Copy">
                      <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" style="width: 16px; height: 16px;">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
                      </svg>
                    </button>
                  </div>
                </div>
              {/if}
            {/each}
          </div>
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

    <!-- Installation History Card -->
    <div class="card">
      <div class="card-header collapsible" on:click={() => installationHistoryExpanded = !installationHistoryExpanded}>
        <h3>Installation History</h3>
        <svg xmlns="http://www.w3.org/2000/svg" class="collapse-icon" class:expanded={installationHistoryExpanded} fill="none" viewBox="0 0 24 24" stroke="currentColor" width="20" height="20">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
        </svg>
      </div>
      {#if installationHistoryExpanded}
      <div class="card-body">
        {#if loadingInstallationHistory}
          <p>Loading installation history...</p>
        {:else if installationHistory.length === 0}
          <p class="no-installations">No installation history found.</p>
        {:else}
          <div class="installation-history-list">
            {#each installationHistory as installation}
              <div class="installation-item">
                <div class="installation-header">
                  <div class="installation-title">
                    <strong>{installation.os_name || installation.template_id || 'Unknown OS'}</strong>
                    <span class="status-badge" 
                          class:pending={installation.status === 'pending'}
                          class:in-progress={installation.status === 'in_progress'}
                          class:completed={installation.status === 'completed'}
                          class:failed={installation.status === 'failed'}
                          class:cancelled={installation.status === 'cancelled'}>
                      {installation.status.replace('_', ' ')}
                    </span>
                  </div>
                  <div class="installation-meta">
                    {#if installation.created_at}
                      <small>Created: {new Date(installation.created_at).toLocaleString()}</small>
                    {/if}
                    {#if installation.completed_at}
                      <small>Completed: {new Date(installation.completed_at).toLocaleString()}</small>
                    {/if}
                  </div>
                </div>
                {#if installation.logs}
                  <details class="installation-logs">
                    <summary>View Logs</summary>
                    <pre class="installation-logs-content">{installation.logs}</pre>
                  </details>
                {/if}
                {#if installation.error_message}
                  <div class="installation-error">
                    <strong>Error:</strong> {installation.error_message}
                  </div>
                {/if}
              </div>
            {/each}
          </div>
        {/if}
      </div>
      {/if}
    </div>
  </div>
{/if}
</div>

<style>
  .content-body {
    padding: 20px;
    max-width: 1400px;
    margin: 0 auto;
    background: var(--bg-secondary);
    color: var(--text-primary);
    transition: background-color 0.3s ease, color 0.3s ease;
  }

  .top-section {
    display: grid;
    grid-template-columns: 1fr 400px;
    gap: 16px;
    margin-bottom: 16px;
  }

  @media (max-width: 1200px) {
    .top-section {
      grid-template-columns: 1fr;
    }
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
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    box-shadow: var(--shadow-sm);
    margin-bottom: 16px;
    overflow: hidden;
    transition: background-color 0.3s ease, border-color 0.3s ease;
  }

  .card-header {
    padding: 12px 16px;
    border-bottom: 1px solid var(--border-color);
    display: flex;
    justify-content: space-between;
    align-items: center;
    background: var(--bg-primary);
    transition: background-color 0.3s ease, border-color 0.3s ease;
  }

  .card-header.collapsible {
    cursor: pointer;
    user-select: none;
  }

  .card-header.collapsible:hover {
    background: var(--bg-tertiary);
  }

  .collapse-icon {
    transition: transform 0.2s ease;
    color: var(--text-secondary);
  }

  .collapse-icon.expanded {
    transform: rotate(180deg);
  }

  .card-header h2 {
    margin: 0;
    font-size: 20px;
    font-weight: 700;
    color: var(--text-primary);
  }

  .card-header h3 {
    margin: 0;
    font-size: 16px;
    font-weight: 600;
    color: var(--text-primary);
  }


  .refresh-button {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 16px;
    background: var(--accent-color);
    border: 1px solid var(--accent-color);
    border-radius: 8px;
    color: white;
    font-size: 14px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.2s ease;
    white-space: nowrap;
  }

  .refresh-button:hover:not(:disabled) {
    background: var(--accent-dark);
    border-color: var(--accent-dark);
    transform: translateY(-1px);
    box-shadow: var(--shadow-md);
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
    padding: 16px;
  }

  .info-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 12px;
  }

  .info-item-full {
    grid-column: 1 / -1;
  }

  .info-item {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .info-item label {
    font-size: 11px;
    font-weight: 700;
    color: var(--text-primary);
    text-transform: uppercase;
    letter-spacing: 0.5px;
    opacity: 0.9;
  }

  .info-item span {
    font-size: 14px;
    color: var(--text-primary);
    font-weight: 500;
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
    background: var(--success-bg);
    color: var(--success-text);
  }

  .status-badge.disabled {
    background: var(--danger-bg);
    color: var(--danger-text);
  }

  .power-control-section {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .power-state-display {
    display: flex;
    align-items: center;
    gap: 12px;
  }

  .power-state-display label {
    font-weight: 600;
    color: var(--text-primary);
  }

  .power-state-badge {
    padding: 6px 16px;
    border-radius: 12px;
    font-size: 14px;
    font-weight: 600;
    text-transform: uppercase;
  }

  .power-state-badge.power-on {
    background: var(--success-bg);
    color: var(--success-text);
  }

  .power-state-badge.power-off {
    background: var(--danger-bg);
    color: var(--danger-text);
  }

  .power-state-badge.power-unknown {
    background: var(--bg-tertiary);
    color: var(--text-secondary);
  }

  .power-control-buttons {
    display: flex;
    gap: 12px;
    flex-wrap: wrap;
  }

  .btn-power {
    padding: 8px 16px;
    border: none;
    border-radius: 6px;
    cursor: pointer;
    transition: all 0.2s ease;
    display: flex;
    align-items: center;
    gap: 6px;
    font-weight: 600;
    font-size: 13px;
    flex: 1;
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
    background: var(--bg-tertiary);
    border: 1px solid var(--border-color);
    padding: 6px;
    cursor: pointer;
    border-radius: 6px;
    transition: all 0.2s ease;
    color: var(--text-primary);
  }

  .btn-icon-only:hover {
    background: var(--bg-secondary);
    border-color: var(--accent-color);
    color: var(--accent-color);
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
    padding: 8px 14px;
    border-radius: 8px;
    font-size: 13px;
    font-weight: 600;
    border: 1px solid var(--border-color);
    transition: all 0.2s ease;
  }

  .capability-badge.tested {
    background: var(--success-bg);
    color: var(--success-text);
    border-color: var(--success-color);
  }
  
  .capability-badge:not(.tested) {
    background: var(--bg-tertiary);
    color: var(--text-primary);
  }
  
  .capability-badge:hover {
    transform: translateY(-1px);
    box-shadow: var(--shadow-sm);
  }

  .capability-icon {
    width: 14px;
    height: 14px;
  }

  .test-logs-section {
    margin-top: 12px;
    padding-top: 12px;
    border-color: var(--border-color);
  }

  .test-logs-content {
    margin-top: 8px;
    padding: 12px;
    background: var(--bg-tertiary);
    border-radius: 6px;
    font-size: 11px;
    font-family: 'Courier New', monospace;
    white-space: pre-wrap;
    word-wrap: break-word;
    max-height: 300px;
    overflow-y: auto;
  }

  .no-capabilities {
    padding: 20px;
    text-align: center;
    color: var(--text-secondary);
  }

  .data-table {
    width: 100%;
    border-collapse: collapse;
  }

  .hardware-section {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .hardware-subsection {
    border-color: var(--border-color);
    padding-bottom: 12px;
  }

  .hardware-subsection:last-child {
    border-bottom: none;
    padding-bottom: 0;
  }

  .subsection-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 12px;
  }

  .subsection-header h4 {
    margin: 0;
    font-size: 14px;
    font-weight: 600;
    color: var(--text-primary);
  }

  .hardware-subsection h4 {
    margin: 0 0 12px 0;
    font-size: 14px;
    font-weight: 600;
    color: var(--text-primary);
  }

  .subsection-content {
    font-size: 13px;
  }

  .data-table {
    font-size: 13px;
  }

  .data-table.compact-table th {
    text-align: left;
    padding: 8px;
    font-weight: 600;
    color: var(--text-primary);
    border-color: var(--border-color);
    font-size: 12px;
  }

  .data-table.compact-table td {
    padding: 8px;
    border-color: var(--border-color);
    color: var(--text-primary);
    font-size: 13px;
  }

  .data-table th {
    text-align: left;
    padding: 10px;
    font-weight: 600;
    color: var(--text-primary);
    border-color: var(--border-color);
    font-size: 13px;
  }

  .data-table td {
    padding: 10px;
    border-color: var(--border-color);
    color: var(--text-primary);
    font-size: 13px;
  }

  .data-table tr:hover {
    background: var(--bg-tertiary);
  }

  .btn-primary {
    padding: 10px 20px;
    background: var(--accent-color);
    color: white;
    border: 1px solid var(--accent-color);
    border-radius: 8px;
    cursor: pointer;
    font-weight: 600;
    transition: all 0.2s ease;
  }

  .btn-primary:hover:not(:disabled) {
    background: var(--accent-dark);
    border-color: var(--accent-dark);
    transform: translateY(-1px);
    box-shadow: var(--shadow-md);
  }

  .btn-secondary {
    padding: 10px 20px;
    background: var(--bg-primary);
    color: var(--text-primary);
    border: 2px solid var(--accent-color);
    border-radius: 8px;
    cursor: pointer;
    font-weight: 600;
    transition: all 0.2s ease;
    box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
  }
  
  .btn-secondary:hover:not(:disabled) {
    background: var(--accent-color);
    border-color: white;
    color: white;
    transform: translateY(-1px);
    box-shadow: var(--shadow-md);
  }

  .btn-secondary:hover {
    background: #e5e7eb;
  }

  .btn-text {
    background: none;
    border: none;
    color: var(--accent-color);
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
  
  .boot-task-status strong {
    color: var(--text-primary);
    font-weight: 700;
  }

  .boot-task-details {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 12px;
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    font-size: 14px;
  }

  .boot-task-details div {
    display: flex;
    gap: 8px;
    color: var(--text-primary);
  }
  
  .boot-task-details div span {
    color: var(--text-primary);
    font-weight: 500;
  }

  .boot-task-details strong {
    min-width: 100px;
    color: var(--text-primary);
    font-weight: 700;
  }

  .status-badge {
    padding: 4px 12px;
    border-radius: 12px;
    font-size: 12px;
    font-weight: 600;
    text-transform: capitalize;
  }

  .status-badge.pending {
    background: var(--warning-bg);
    color: var(--warning-text);
  }

  .status-badge.in-progress {
    background: var(--info-bg);
    color: var(--info-text);
  }

  .status-badge.completed {
    background: var(--success-bg);
    color: var(--success-text);
  }

  .status-badge.failed {
    background: var(--danger-bg);
    color: var(--danger-text);
  }

  .boot-options-section {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
    gap: 16px;
  }

  .boot-option-group {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  .boot-option-group h4,
  .boot-option h4 {
    margin: 0 0 6px 0;
    font-size: 14px;
    font-weight: 600;
    color: var(--text-primary);
  }

  .boot-option-description {
    color: var(--text-secondary);
    font-size: 12px;
    margin: 0 0 10px 0;
  }

  .no-isos {
    color: var(--text-secondary);
    font-size: 14px;
  }

  .iso-selector {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .iso-selector label {
    font-weight: 600;
    color: var(--text-primary);
    font-size: 14px;
  }

  .iso-selector select {
    padding: 10px 12px;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    font-size: 14px;
    font-family: inherit;
    background: var(--bg-primary);
    color: var(--text-primary);
    cursor: pointer;
    transition: border-color 0.2s ease, background-color 0.3s ease, color 0.3s ease;
  }

  .iso-selector select:focus {
    outline: none;
    border-color: var(--accent-color);
    box-shadow: 0 0 0 3px rgba(8, 145, 178, 0.1);
  }

  .iso-info {
    color: var(--text-secondary);
    font-size: 13px;
  }

  .template-info {
    margin: 12px 0;
    padding: 12px;
    background: var(--bg-tertiary);
    border-radius: 6px;
    border-left: 3px solid var(--accent-color);
  }

  .template-description {
    color: var(--text-secondary);
    margin-bottom: 12px;
    font-size: 12px;
  }

  .template-parameters-form {
    margin-top: 16px;
  }

  .template-parameters-form h5 {
    margin: 0 0 12px 0;
    color: var(--text-primary);
    font-size: 14px;
    font-weight: 600;
  }

  .template-parameters-form .form-group {
    margin-bottom: 12px;
  }

  .template-parameters-form label {
    display: block;
    margin-bottom: 4px;
    font-weight: 600;
    color: var(--text-primary);
    font-size: 13px;
  }

  .template-parameters-form .required {
    color: #ef4444;
  }

  .template-parameters-form input[type="text"],
  .template-parameters-form input[type="password"],
  .template-parameters-form input[type="number"],
  .template-parameters-form select {
    width: 100%;
    padding: 6px 10px;
    border-color: var(--border-color);
    border-radius: 6px;
    font-size: 13px;
    transition: border-color 0.2s ease;
  }

  .template-parameters-form input:focus,
  .template-parameters-form select:focus {
    outline: none;
    border-color: var(--accent-color);
  }

  .template-parameters-form .checkbox-label {
    display: flex;
    align-items: center;
    gap: 8px;
    cursor: pointer;
    font-weight: normal;
  }

  .template-parameters-form .checkbox-label input[type="checkbox"] {
    width: auto;
    cursor: pointer;
  }

  .field-help {
    display: block;
    margin-top: 4px;
    font-size: 12px;
    color: var(--text-secondary);
  }

  .btn-small {
    padding: 6px 12px;
    font-size: 13px;
    min-width: auto;
  }

  .credentials-info {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .credential-item {
    padding: 10px;
    background: var(--bg-tertiary);
    border-radius: 6px;
    border-left: 3px solid var(--accent-color);
  }

  .credential-item strong {
    display: block;
    margin-bottom: 4px;
    color: var(--text-primary);
    font-size: 13px;
  }

  .credential-item code {
    word-break: break-all;
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
  
  .boot-task-status strong {
    color: var(--text-primary);
    font-weight: 700;
  }

  .boot-task-details {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 12px;
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    font-size: 14px;
  }

  .boot-task-details div {
    display: flex;
    gap: 8px;
    color: var(--text-primary);
  }
  
  .boot-task-details div span {
    color: var(--text-primary);
    font-weight: 500;
  }

  .boot-task-details strong {
    min-width: 100px;
    color: var(--text-primary);
    font-weight: 700;
  }

  .status-badge {
    padding: 4px 12px;
    border-radius: 12px;
    font-size: 12px;
    font-weight: 600;
    text-transform: capitalize;
  }

  .status-badge.pending {
    background: var(--warning-bg);
    color: var(--warning-text);
  }

  .status-badge.in-progress {
    background: var(--info-bg);
    color: var(--info-text);
  }

  .status-badge.completed {
    background: var(--success-bg);
    color: var(--success-text);
  }

  .status-badge.failed {
    background: var(--danger-bg);
    color: var(--danger-text);
  }

  .no-isos {
    color: var(--text-secondary);
    font-size: 14px;
  }

  .iso-selector {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .iso-selector label {
    font-weight: 600;
    color: var(--text-primary);
    font-size: 14px;
  }

  .iso-selector select {
    padding: 10px 12px;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    font-size: 14px;
    font-family: inherit;
    background: var(--bg-primary);
    color: var(--text-primary);
    cursor: pointer;
    transition: border-color 0.2s ease, background-color 0.3s ease, color 0.3s ease;
  }

  .iso-selector select:focus {
    outline: none;
    border-color: var(--accent-color);
    box-shadow: 0 0 0 3px rgba(8, 145, 178, 0.1);
  }

  .iso-info {
    color: var(--text-secondary);
    font-size: 13px;
  }

  .no-installations {
    color: var(--text-secondary);
    font-size: 13px;
    padding: 12px;
    text-align: center;
  }

  .installation-history-list {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .installation-item {
    padding: 12px;
    background: var(--bg-tertiary);
    border-radius: 6px;
    border-left: 3px solid var(--accent-color);
  }

  .installation-header {
    display: flex;
    flex-direction: column;
    gap: 6px;
    margin-bottom: 8px;
  }

  .installation-title {
    display: flex;
    align-items: center;
    gap: 10px;
    font-size: 14px;
  }

  .installation-meta {
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-size: 12px;
    color: var(--text-secondary);
  }

  .installation-logs {
    margin-top: 8px;
  }

  .installation-logs summary {
    cursor: pointer;
    font-size: 13px;
    font-weight: 600;
    color: var(--accent-color);
    padding: 4px 0;
  }

  .installation-logs-content {
    margin-top: 8px;
    padding: 10px;
    background: #1e293b;
    color: #e2e8f0;
    border-radius: 6px;
    font-size: 11px;
    font-family: 'Courier New', monospace;
    white-space: pre-wrap;
    word-wrap: break-word;
    max-height: 400px;
    overflow-y: auto;
  }

  .installation-error {
    margin-top: 8px;
    padding: 8px;
    background: #fee2e2;
    border-radius: 6px;
    font-size: 13px;
    color: #991b1b;
  }
</style>
