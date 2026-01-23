<script>
  import PageHeader from './PageHeader.svelte';
  import { getServers, createServer, updateServer, deleteServer, getPlugins, getLocations, getRacks, testServerConnection, testServerCapabilities, testPluginCapabilities } from '../lib/api.js';
  import { onMount } from 'svelte';
  import { link } from 'svelte-spa-router';

  let servers = [];
  let plugins = [];
  let locations = [];
  let racks = [];
  let availableRacks = [];
  let loading = true;
  let error = null;
  let showModal = false;
  let editingServer = null;
  let formData = {
    name: '',
    server_ip: '',
    description: '',
    cpu_count: 1,
    cpu_model: '',
    ram_gb: null,
    location_id: null,
    rack_id: null,
    rack_unit: null,
    plugin_id: null,
    plugin_config: {},
    boot_mode: 'uefi', // Deprecated - kept for backward compatibility
    pxe_boot_mode: 'uefi', // Controls what DHCP serves initially
    os_boot_mode: 'uefi', // Controls how the server boots the installed OS
    disks: [],
    network_ports: [],
    // IPMI Web Management configuration
    ipmi_web_management_url: '',
    ipmi_viewer_username: '',
    ipmi_viewer_password: ''
  };
  let formError = null;
  let pluginConfigError = null;
  let testingConnection = false;
  let testResult = null;
  let testPassed = false;
  let testingCapabilities = {}; // Map of server_id -> boolean

  onMount(async () => {
    await Promise.all([loadServers(), loadPlugins(), loadLocations(), loadRacks()]);
  });

  async function loadServers() {
    try {
      loading = true;
      error = null;
      servers = await getServers();
    } catch (err) {
      error = err.message;
      console.error('Failed to load servers:', err);
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

  async function loadRacks() {
    try {
      racks = await getRacks();
      updateAvailableRacks();
    } catch (err) {
      console.error('Failed to load racks:', err);
    }
  }

  function updateAvailableRacks() {
    if (formData.location_id) {
      // Convert to number for comparison (select elements return strings)
      const locationId = Number(formData.location_id);
      availableRacks = racks.filter(r => Number(r.location_id) === locationId);
      // Clear rack selection if current rack is not in the selected location
      if (formData.rack_id) {
        const currentRack = racks.find(r => r.id === formData.rack_id);
        if (!currentRack || Number(currentRack.location_id) !== locationId) {
          formData.rack_id = null;
          formData.rack_unit = null;
        }
      }
    } else {
      availableRacks = [];
      formData.rack_id = null;
      formData.rack_unit = null;
    }
  }

  function onLocationChange() {
    updateAvailableRacks();
  }

  function getMaxRackUnits() {
    if (!formData.rack_id) return 0;
    const rack = racks.find(r => r.id === formData.rack_id);
    return rack ? rack.units : 0;
  }

  // Reactive: update available racks when location_id or racks change
  $: if (formData.location_id && racks.length > 0) {
    updateAvailableRacks();
  }

  function openAddModal() {
    editingServer = null;
    previousPluginId = null;
    formData = {
      name: '',
      server_ip: '',
      description: '',
      cpu_count: 1,
      cpu_model: '',
      ram_gb: null,
      location_id: null,
      rack_id: null,
      rack_unit: null,
      plugin_id: null,
      plugin_config: {},
      boot_mode: 'uefi', // Deprecated - kept for backward compatibility
      pxe_boot_mode: 'uefi', // Controls what DHCP serves initially
      os_boot_mode: 'uefi', // Controls how the server boots the installed OS
      disks: [],
      network_ports: [],
      // IPMI Web Management configuration
      ipmi_web_management_url: '',
      ipmi_viewer_username: '',
      ipmi_viewer_password: ''
    };
    formError = null;
    pluginConfigError = null;
    testResult = null;
    testPassed = false;
    capabilitiesTestResult = null;
    capabilitiesTestPassed = false;
    testingCapabilitiesInForm = false;
    showModal = true;
  }

  function openEditModal(server) {
    editingServer = server;
    previousPluginId = server.plugin_id;
    formData = {
      name: server.name,
      server_ip: server.server_ip,
      description: server.description || '',
      cpu_count: server.cpu_count,
      cpu_model: server.cpu_model || '',
      ram_gb: server.ram_gb,
      boot_mode: server.boot_mode || 'uefi', // Deprecated - kept for backward compatibility
      pxe_boot_mode: server.pxe_boot_mode || server.boot_mode || 'uefi', // Fallback to boot_mode for backward compatibility
      os_boot_mode: server.os_boot_mode || server.boot_mode || 'uefi', // Fallback to boot_mode for backward compatibility
      location_id: server.location_id,
      rack_id: server.rack_id || null,
      rack_unit: server.rack_unit || null,
      plugin_id: server.plugin_id,
      plugin_config: server.plugin_config || {},
      // IPMI Web Proxy configuration
      ipmi_web_management_url: server.ipmi_web_management_url || '',
      ipmi_viewer_username: server.ipmi_viewer_username || '',
      ipmi_viewer_password: server.ipmi_viewer_password || '',
      disks: (server.disks || []).map(d => ({
        type: d.type,
        capacity_gb: d.capacity_gb,
        description: d.description || '',
        serial_number: d.serial_number || '',
        is_os_disk: d.is_os_disk || false
      })),
      network_ports: (server.network_ports || []).map(p => ({
        name: p.name,
        mac_address: p.mac_address || '',
        speed_mbps: p.speed_mbps,
        lag_group: p.lag_group || '',
        monitor_bandwidth: p.monitor_bandwidth || false,
        pxe_boot: p.pxe_boot || false,
        pxe_ip: p.pxe_ip || '',
        description: p.description || ''
      }))
    };
    formError = null;
    pluginConfigError = null;
    testResult = null;
    testPassed = false;
    capabilitiesTestResult = null;
    capabilitiesTestPassed = false;
    testingCapabilitiesInForm = false;
    // Update available racks when opening edit modal (after formData is set)
    updateAvailableRacks();
    showModal = true;
  }

  function closeModal() {
    showModal = false;
    editingServer = null;
    formError = null;
    pluginConfigError = null;
  }

  function addDisk() {
    formData.disks = [...formData.disks, { type: 'ssd', capacity_gb: 0, description: '', serial_number: '', is_os_disk: false }];
  }

  function removeDisk(index) {
    formData.disks = formData.disks.filter((_, i) => i !== index);
  }

  function addNetworkPort() {
    formData.network_ports = [...formData.network_ports, { 
      name: '', 
      mac_address: '',
      speed_mbps: 1000, 
      lag_group: '', 
      monitor_bandwidth: false,
      pxe_boot: false,
      pxe_ip: '',
      description: '' 
    }];
  }

  function handlePxeBootChange(portIndex, checked) {
    // If checking this port as PXE boot, uncheck all other ports
    if (checked) {
      formData.network_ports.forEach((port, index) => {
        if (index !== portIndex) {
          port.pxe_boot = false;
        }
      });
    }
    formData.network_ports[portIndex].pxe_boot = checked;
    // If unchecking, clear the PXE IP
    if (!checked) {
      formData.network_ports[portIndex].pxe_ip = '';
    }
  }

  function copyServerIpToPxe(portIndex) {
    if (formData.server_ip) {
      formData.network_ports[portIndex].pxe_ip = formData.server_ip;
    }
  }

  function removeNetworkPort(index) {
    formData.network_ports = formData.network_ports.filter((_, i) => i !== index);
  }

  function getSelectedPlugin() {
    if (!formData.plugin_id) return null;
    return plugins.find(p => p.id === formData.plugin_id);
  }

  let previousPluginId = null;
  let testingCapabilitiesInForm = false;
  let capabilitiesTestResult = null;
  let capabilitiesTestPassed = false;

  function updatePluginConfig() {
    const plugin = getSelectedPlugin();
    
    // If plugin changed, clear config and test results
    if (previousPluginId !== null && previousPluginId !== formData.plugin_id) {
      formData.plugin_config = {};
      testResult = null;
      testPassed = false;
      capabilitiesTestResult = null;
      capabilitiesTestPassed = false;
      pluginConfigError = null;
    }
    previousPluginId = formData.plugin_id;
    
    if (!plugin) {
      formData.plugin_config = {};
      return;
    }
    
    if (!plugin.config_template) {
      formData.plugin_config = {};
      return;
    }

    // Preserve existing config values if they exist, otherwise use defaults
    const existingConfig = formData.plugin_config || {};
    const config = {};
    const properties = plugin.config_template.properties || {};
    
    for (const [key, schema] of Object.entries(properties)) {
      // If value already exists in formData, preserve it (user may have changed it)
      if (existingConfig.hasOwnProperty(key)) {
        config[key] = existingConfig[key];
      } else if (schema.type === 'boolean') {
        // For booleans, use the schema default if available, otherwise false
        config[key] = schema.default !== undefined ? schema.default : false;
      } else if (schema.default !== undefined) {
        config[key] = schema.default;
      } else if (schema.type === 'integer') {
        config[key] = schema.default || 0;
      } else {
        config[key] = '';
      }
    }
    
    formData.plugin_config = config;
    
    // Clear any existing test results when plugin changes
    testResult = null;
    testPassed = false;
    capabilitiesTestResult = null;
    capabilitiesTestPassed = false;
  }

  async function testConnection() {
    const plugin = getSelectedPlugin();
    if (!plugin) {
      pluginConfigError = 'Please select a plugin first';
      return;
    }

    if (!formData.plugin_config || Object.keys(formData.plugin_config).length === 0) {
      pluginConfigError = 'Please configure the plugin settings';
      return;
    }

    // Validate required fields
    const requiredFields = plugin.config_template?.required || [];
    const missingFields = requiredFields.filter(field => {
      const value = formData.plugin_config[field];
      return value === undefined || value === null || value === '';
    });
    
    if (missingFields.length > 0) {
      pluginConfigError = `Missing required fields: ${missingFields.join(', ')}`;
      testResult = null;
      testPassed = false;
      return;
    }

    try {
      testingConnection = true;
      pluginConfigError = null;
      testResult = null;
      
      const result = await testServerConnection(formData.plugin_id, formData.plugin_config);
      testResult = result;
      testPassed = result.success === true;
      
      if (!testPassed) {
        pluginConfigError = result.message || 'Connection test failed';
      }
    } catch (err) {
      testResult = {
        success: false,
        message: err.message || 'Connection test failed'
      };
      testPassed = false;
      pluginConfigError = err.message || 'Connection test failed';
    } finally {
      testingConnection = false;
    }
  }

  async function handleSubmit() {
    if (!formData.name.trim()) {
      formError = 'Name is required';
      return;
    }
    if (!formData.server_ip.trim()) {
      formError = 'Server IP is required';
      return;
    }
    if (!formData.location_id) {
      formError = 'Location is required';
      return;
    }
    if (!formData.plugin_id) {
      formError = 'Plugin is required';
      return;
    }
    if (!formData.plugin_config || Object.keys(formData.plugin_config).length === 0) {
      formError = 'Plugin configuration is required';
      return;
    }

    // Validate disks
    for (let i = 0; i < formData.disks.length; i++) {
      const disk = formData.disks[i];
      if (disk.capacity_gb <= 0) {
        formError = `Disk ${i + 1}: Capacity must be greater than 0`;
        return;
      }
    }

    try {
      formError = null;
      const submitData = {
        ...formData,
        disks: formData.disks.map(d => ({
          type: d.type,
          capacity_gb: parseInt(d.capacity_gb),
          description: d.description || null,
          serial_number: d.serial_number && d.serial_number.trim() ? d.serial_number.trim() : null,
          is_os_disk: d.is_os_disk || false
        })),
        network_ports: formData.network_ports.map(p => ({
          name: p.name.trim(),
          mac_address: p.mac_address && p.mac_address.trim() ? p.mac_address.trim() : null,
          speed_mbps: parseInt(p.speed_mbps),
          lag_group: p.lag_group && p.lag_group.trim() ? p.lag_group.trim() : null,
          monitor_bandwidth: p.monitor_bandwidth || false,
          pxe_boot: p.pxe_boot || false,
          pxe_ip: p.pxe_boot && p.pxe_ip && p.pxe_ip.trim() ? p.pxe_ip.trim() : null,
          description: p.description && p.description.trim() ? p.description.trim() : null
        }))
      };

      if (editingServer) {
        await updateServer(editingServer.id, submitData);
      } else {
        await createServer(submitData);
      }
      closeModal();
      await loadServers();
    } catch (err) {
      formError = err.message;
    }
  }

  async function handleDelete(server) {
    if (!confirm(`Are you sure you want to delete server "${server.name}"?`)) {
      return;
    }

    try {
      await deleteServer(server.id);
      await loadServers();
    } catch (err) {
      alert('Failed to delete server: ' + err.message);
    }
  }

  function getAvailableCapabilities(server) {
    const plugin = plugins.find(p => p.id === server.plugin_id);
    return plugin?.available_capabilities || [];
  }
</script>

<PageHeader title="Servers" />

<div class="servers-container">
  <div class="servers-header">
    <h2>Servers</h2>
    <button class="btn-primary" on:click={openAddModal}>
      <svg xmlns="http://www.w3.org/2000/svg" class="btn-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
      </svg>
      Add Server
    </button>
  </div>

  {#if loading}
    <div class="loading">Loading servers...</div>
  {:else if error}
    <div class="error">Error: {error}</div>
  {:else if servers.length === 0}
    <div class="empty-state">
      <p>No servers found. Click "Add Server" to create one.</p>
    </div>
  {:else}
    <div class="servers-table">
      <table>
        <thead>
          <tr>
            <th>Name</th>
            <th>Server IP</th>
            <th>CPU</th>
            <th>RAM</th>
            <th>Location</th>
            <th>Plugin</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          {#each servers as server}
            <tr>
              <td>
                <a href="/admin/servers/{server.id}" class="server-name-link">
                  <strong>{server.name}</strong>
                </a>
              </td>
              <td>{server.server_ip}</td>
              <td>{server.cpu_count}x {server.cpu_model || 'N/A'}</td>
              <td>{server.ram_gb ? server.ram_gb + ' GB' : 'N/A'}</td>
              <td>{server.location_id ? (locations.find(l => l.id === server.location_id)?.name || 'N/A') : 'N/A'}</td>
              <td>{plugins.find(p => p.id === server.plugin_id)?.name || 'N/A'}</td>
              <td>
                <div class="table-actions">
                  <a href="/admin/servers/{server.id}" class="btn-icon-only" title="View Details">
                    <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z" />
                    </svg>
                  </a>
                  <button class="btn-icon-only" on:click={() => openEditModal(server)} title="Edit">
                    <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
                    </svg>
                  </button>
                  <button class="btn-icon-only btn-danger" on:click={() => handleDelete(server)} title="Delete">
                    <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                    </svg>
                  </button>
                </div>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</div>

{#if showModal}
  <div class="modal-overlay" on:click={closeModal}>
    <div class="modal-content large" on:click|stopPropagation>
      <div class="modal-header">
        <h3>{editingServer ? 'Edit Server' : 'Add Server'}</h3>
        <button class="btn-icon-only" on:click={closeModal}>
          <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
          </svg>
        </button>
      </div>
      <div class="modal-body">
        {#if formError}
          <div class="form-error">{formError}</div>
        {/if}

        <div class="form-section">
          <h4>Basic Information</h4>
          <div class="form-row">
            <div class="form-group">
              <label for="server-name">Name *</label>
              <input id="server-name" type="text" bind:value={formData.name} required />
            </div>
            <div class="form-group">
              <label for="server-ip">Server IP *</label>
              <input id="server-ip" type="text" bind:value={formData.server_ip} placeholder="192.168.1.100" required />
            </div>
          </div>
          <div class="form-group">
            <label for="server-description">Description</label>
            <textarea id="server-description" bind:value={formData.description} rows="2"></textarea>
          </div>
        </div>

        <div class="form-section">
          <h4>Hardware Specifications</h4>
          <div class="form-row">
            <div class="form-group">
              <label for="cpu-count">CPU Count *</label>
              <input id="cpu-count" type="number" bind:value={formData.cpu_count} min="1" required />
            </div>
            <div class="form-group">
              <label for="cpu-model">CPU Model</label>
              <input id="cpu-model" type="text" bind:value={formData.cpu_model} placeholder="e.g., Intel Xeon E5-2680" />
            </div>
          </div>
          <div class="form-row">
            <div class="form-group">
              <label for="ram-gb">RAM (GB)</label>
              <input id="ram-gb" type="number" bind:value={formData.ram_gb} min="0" />
            </div>
            <div class="form-group">
              <label for="pxe-boot-mode">PXE Boot Mode *</label>
              <select id="pxe-boot-mode" bind:value={formData.pxe_boot_mode} required>
                <option value="uefi">UEFI</option>
                <option value="bios">BIOS</option>
              </select>
              <small class="field-help">Controls what DHCP serves initially (UEFI: snponly.efi, BIOS: undionly.kpxe)</small>
            </div>
            <div class="form-group">
              <label for="os-boot-mode">OS Boot Mode *</label>
              <select id="os-boot-mode" bind:value={formData.os_boot_mode} required>
                <option value="uefi">UEFI</option>
                <option value="bios">BIOS</option>
              </select>
              <small class="field-help">Controls how the server boots the installed OS</small>
            </div>
          </div>
        </div>

        <div class="form-section">
          <h4>Location</h4>
          <div class="form-group">
            <label for="location">Location *</label>
            <select id="location" bind:value={formData.location_id} on:change={onLocationChange} required>
              <option value={null}>Select a location</option>
              {#each locations as location}
                <option value={location.id}>{location.name}</option>
              {/each}
            </select>
          </div>
        </div>

        <div class="form-section">
          <h4>Rack Assignment (Optional)</h4>
          <div class="form-row">
            <div class="form-group">
              <label for="rack">Rack</label>
              <select id="rack" bind:value={formData.rack_id} disabled={!formData.location_id || availableRacks.length === 0}>
                <option value={null}>No rack</option>
                {#each availableRacks as rack}
                  <option value={rack.id}>{rack.name} ({rack.units}U)</option>
                {/each}
              </select>
              {#if formData.location_id && availableRacks.length === 0}
                <small class="field-help">No racks available in this location</small>
              {/if}
            </div>
            <div class="form-group">
              <label for="rack-unit">Rack Unit (U)</label>
              <input 
                id="rack-unit" 
                type="number" 
                bind:value={formData.rack_unit} 
                min="1" 
                max={getMaxRackUnits()}
                disabled={!formData.rack_id}
                placeholder="1-{getMaxRackUnits()}"
              />
              {#if formData.rack_id}
                <small class="field-help">Unit position (1-{getMaxRackUnits()})</small>
              {/if}
            </div>
          </div>
        </div>

        <div class="form-section">
          <h4>Management Plugin</h4>
          <div class="form-group">
            <label for="plugin">Plugin *</label>
            <select id="plugin" bind:value={formData.plugin_id} on:change={updatePluginConfig}>
              <option value={null}>Select a plugin</option>
              {#each plugins.filter(p => p.id !== null && p.id !== undefined) as plugin}
                <option value={plugin.id}>{plugin.name} (v{plugin.version})</option>
              {/each}
            </select>
            {#if plugins.filter(p => p.id === null || p.id === undefined).length > 0}
              <small class="field-help" style="color: #ef4444;">
                Some plugins are not registered. Please sync plugins first.
              </small>
            {/if}
          </div>

          {#if formData.plugin_id}
            {@const plugin = getSelectedPlugin()}
            {#if plugin && plugin.config_template && plugin.config_template.properties && Object.keys(plugin.config_template.properties).length > 0}
              {@const properties = plugin.config_template.properties || {}}
              <div class="plugin-config">
                <h5>Plugin Configuration</h5>
                {#if pluginConfigError}
                  <div class="form-error">{pluginConfigError}</div>
                {/if}
                {#each Object.entries(properties) as [key, schema]}
                  <div class="form-group">
                    <label for="config-{key}">
                      {schema.title || key}
                      {#if (plugin.config_template.required || []).includes(key)}
                        <span class="required">*</span>
                      {/if}
                    </label>
                    {#if schema.type === 'boolean'}
                      <input
                        id="config-{key}"
                        type="checkbox"
                        checked={formData.plugin_config[key] === true}
                        on:change={(e) => {
                          formData.plugin_config = {
                            ...formData.plugin_config,
                            [key]: e.target.checked
                          };
                        }}
                      />
                    {:else if schema.type === 'integer'}
                      <input
                        id="config-{key}"
                        type="number"
                        bind:value={formData.plugin_config[key]}
                        placeholder={schema.default || ''}
                      />
                    {:else if schema.format === 'password'}
                      <input
                        id="config-{key}"
                        type="password"
                        bind:value={formData.plugin_config[key]}
                        placeholder={schema.description || ''}
                      />
                    {:else}
                      <input
                        id="config-{key}"
                        type="text"
                        bind:value={formData.plugin_config[key]}
                        placeholder={schema.description || schema.default || ''}
                      />
                    {/if}
                    {#if schema.description}
                      <small class="field-help">{schema.description}</small>
                    {/if}
                  </div>
                {/each}
                <div class="plugin-test-section">
                  <div class="test-controls">
                    <button type="button" class="btn-secondary" on:click={testConnection} disabled={testingConnection || !formData.plugin_id}>
                      {testingConnection ? 'Testing...' : 'Test Connection'}
                    </button>
                  </div>
                  {#if testResult}
                    <div class="test-result" class:test-success={testResult.success} class:test-failure={!testResult.success}>
                      <div class="test-result-header">
                        {#if testResult.success}
                          <svg xmlns="http://www.w3.org/2000/svg" class="test-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
                          </svg>
                        {:else}
                          <svg xmlns="http://www.w3.org/2000/svg" class="test-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 14l2-2m0 0l2-2m-2 2l-2-2m2 2l2 2m7-2a9 9 0 11-18 0 9 9 0 0118 0z" />
                          </svg>
                        {/if}
                        <strong>{testResult.success ? 'Connection Successful' : 'Connection Failed'}</strong>
                      </div>
                      <div class="test-result-message">{testResult.message}</div>
                      {#if testResult.details && Object.keys(testResult.details).length > 0}
                        <div class="test-result-details">
                          {#each Object.entries(testResult.details) as [key, value]}
                            <div class="test-detail-item">
                              <span class="test-detail-key">{key}:</span>
                              <span class="test-detail-value">{value}</span>
                            </div>
                          {/each}
                        </div>
                      {/if}
                    </div>
                  {/if}
                  {#if capabilitiesTestResult}
                    <div class="test-result" class:test-success={capabilitiesTestResult.success !== false} class:test-failure={capabilitiesTestResult.success === false}>
                      <div class="test-result-header">
                        {#if capabilitiesTestResult.success !== false}
                          <svg xmlns="http://www.w3.org/2000/svg" class="test-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
                          </svg>
                          <strong>Capability Test Results</strong>
                        {:else}
                          <svg xmlns="http://www.w3.org/2000/svg" class="test-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 14l2-2m0 0l2-2m-2 2l-2-2m2 2l2 2m7-2a9 9 0 11-18 0 9 9 0 0118 0z" />
                          </svg>
                          <strong>Capability Test Failed</strong>
                        {/if}
                      </div>
                      {#if capabilitiesTestResult.summary}
                        <div class="capability-summary">
                          <div class="capability-summary-item">
                            <span class="capability-label">Total:</span>
                            <span class="capability-value">{capabilitiesTestResult.summary.total}</span>
                          </div>
                          <div class="capability-summary-item">
                            <span class="capability-label">Tested:</span>
                            <span class="capability-value success">{capabilitiesTestResult.summary.tested}</span>
                          </div>
                          <div class="capability-summary-item">
                            <span class="capability-label">Failed:</span>
                            <span class="capability-value error">{capabilitiesTestResult.summary.failed}</span>
                          </div>
                        </div>
                        {#if capabilitiesTestResult.tested_capabilities && capabilitiesTestResult.tested_capabilities.length > 0}
                          <div class="capability-list">
                            <strong>Available Capabilities:</strong>
                            <div class="capability-badges">
                              {#each capabilitiesTestResult.tested_capabilities as capability}
                                <span class="capability-badge">{capability}</span>
                              {/each}
                            </div>
                          </div>
                        {/if}
                      {/if}
                      {#if capabilitiesTestResult.test_logs}
                        <details class="test-logs-details">
                          <summary>View Test Logs</summary>
                          <pre class="test-logs-content">{capabilitiesTestResult.test_logs}</pre>
                        </details>
                      {/if}
                    </div>
                  {/if}
                </div>
              </div>
            {/if}
          {/if}
        </div>

        <div class="form-section">
          <h4>IPMI Web Management</h4>
          <div class="form-group">
            <label for="ipmi-web-url">Web Management URL</label>
            <input 
              id="ipmi-web-url" 
              type="text" 
              bind:value={formData.ipmi_web_management_url} 
              placeholder="e.g., https://192.168.1.100"
            />
            <small class="field-help">URL for IPMI web management interface</small>
          </div>
          <div class="form-row">
            <div class="form-group">
              <label for="ipmi-viewer-username">Viewer Username</label>
              <input 
                id="ipmi-viewer-username" 
                type="text" 
                bind:value={formData.ipmi_viewer_username} 
                placeholder="Read-only username"
              />
              <small class="field-help">Username for web access</small>
            </div>
            <div class="form-group">
              <label for="ipmi-viewer-password">Viewer Password</label>
              <input 
                id="ipmi-viewer-password" 
                type="password" 
                bind:value={formData.ipmi_viewer_password} 
                placeholder="Read-only password"
              />
              <small class="field-help">Password for web access</small>
            </div>
          </div>
        </div>

        <div class="form-section">
          <div class="section-header">
            <h4>Disks</h4>
            <button type="button" class="btn-secondary btn-small" on:click={addDisk}>
              <svg xmlns="http://www.w3.org/2000/svg" class="btn-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
              </svg>
              Add Disk
            </button>
          </div>
          {#if formData.disks.length === 0}
            <p class="empty-disks">No disks added. Click "Add Disk" to add one.</p>
          {:else}
            <div class="disks-list">
              {#each formData.disks as disk, index}
                <div class="disk-item">
                  <div class="disk-header">
                    <strong>Disk {index + 1}</strong>
                    <button type="button" class="btn-icon-only btn-danger btn-small" on:click={() => removeDisk(index)}>
                      <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
                      </svg>
                    </button>
                  </div>
                  <div class="form-row">
                    <div class="form-group">
                      <label>Type</label>
                      <select bind:value={disk.type}>
                        <option value="ssd">SSD</option>
                        <option value="hdd">HDD</option>
                      </select>
                    </div>
                    <div class="form-group">
                      <label>Capacity (GB) *</label>
                      <input type="number" bind:value={disk.capacity_gb} min="1" required />
                    </div>
                  </div>
                  <div class="form-row">
                    <div class="form-group">
                      <label>Serial Number</label>
                      <input type="text" bind:value={disk.serial_number} placeholder="Optional serial number" />
                      <small class="field-help">Used to match disk during OS installation</small>
                    </div>
                    <div class="form-group">
                      <label style="display: flex; align-items: center; gap: 8px; cursor: pointer;">
                        <input type="checkbox" bind:checked={disk.is_os_disk} style="cursor: pointer;" />
                        <span>OS Installation Disk</span>
                      </label>
                      <small class="field-help">Mark this disk as the target for OS installation</small>
                    </div>
                  </div>
                  <div class="form-group">
                    <label>Description</label>
                    <input type="text" bind:value={disk.description} placeholder="Optional description" />
                  </div>
                </div>
              {/each}
            </div>
          {/if}
        </div>

        <div class="form-section">
          <div class="section-header">
            <h4>Network Ports</h4>
            <button type="button" class="btn-secondary btn-small" on:click={addNetworkPort}>
              <svg xmlns="http://www.w3.org/2000/svg" class="btn-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
              </svg>
              Add Port
            </button>
          </div>
          {#if formData.network_ports.length === 0}
            <p class="empty-disks">No network ports added. Click "Add Port" to add one.</p>
          {:else}
            <div class="disks-list">
              {#each formData.network_ports as port, index}
                <div class="disk-item">
                  <div class="disk-header">
                    <strong>Port {index + 1}</strong>
                    <button type="button" class="btn-icon-only btn-danger btn-small" on:click={() => removeNetworkPort(index)}>
                      <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
                      </svg>
                    </button>
                  </div>
                  <div class="form-row">
                    <div class="form-group">
                      <label>Port Name *</label>
                      <input type="text" bind:value={port.name} placeholder="e.g., eth0, enp1s0, Port 1" required />
                    </div>
                    <div class="form-group">
                      <label>MAC Address</label>
                      <input type="text" bind:value={port.mac_address} placeholder="e.g., 00:0e:1e:6f:16:b0" pattern="^([0-9A-Fa-f]{2}[\-:]){5}[0-9A-Fa-f]{2}$" />
                      <small class="field-help">Format: XX:XX:XX:XX:XX:XX or XX-XX-XX-XX-XX-XX</small>
                    </div>
                  </div>
                  <div class="form-row">
                    <div class="form-group">
                      <label>Speed (Mbps) *</label>
                      <div class="port-speed-input-group">
                        <input type="number" bind:value={port.speed_mbps} min="1" placeholder="e.g., 1000 for 1Gbps" required />
                        <div class="port-speed-quick-buttons">
                          <button type="button" class="btn-quick" on:click={() => port.speed_mbps = 1000}>
                            1 Gbps
                          </button>
                          <button type="button" class="btn-quick" on:click={() => port.speed_mbps = 10000}>
                            10 Gbps
                          </button>
                          <button type="button" class="btn-quick" on:click={() => port.speed_mbps = 25000}>
                            25 Gbps
                          </button>
                          {#if port.speed_mbps}
                            <div class="port-speed-display">
                              {port.speed_mbps.toLocaleString()} Mbps ({port.speed_mbps / 1000} Gbps)
                            </div>
                          {/if}
                        </div>
                      </div>
                    </div>
                  </div>
                  <div class="form-row">
                    <div class="form-group">
                      <label>LAG Group</label>
                      <input type="text" bind:value={port.lag_group} placeholder="e.g., bond0, lag1 (leave empty if not in LAG)" />
                      <small class="field-help">Ports with the same LAG group name will be grouped together</small>
                    </div>
                    <div class="form-group">
                      <label>
                        <input type="checkbox" bind:checked={port.monitor_bandwidth} />
                        Monitor Bandwidth
                      </label>
                      <small class="field-help">Enable bandwidth monitoring for this port</small>
                    </div>
                  </div>
                  <div class="form-row">
                    <div class="form-group">
                      <label>
                        <input type="checkbox" checked={port.pxe_boot} on:change={(e) => handlePxeBootChange(index, e.target.checked)} />
                        PXE Boot Port
                      </label>
                      <small class="field-help">Mark this port as the PXE boot port (only one port can be selected)</small>
                    </div>
                  </div>
                  {#if port.pxe_boot}
                    <div class="form-row">
                      <div class="form-group">
                        <label>PXE Boot IP</label>
                        <div style="display: flex; gap: 8px; align-items: flex-start;">
                          <input type="text" bind:value={port.pxe_ip} placeholder="e.g., 192.168.1.100" style="flex: 1;" />
                          <button type="button" class="btn-secondary btn-small" on:click={() => copyServerIpToPxe(index)} title="Copy Server IP">
                            <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" style="width: 16px; height: 16px;">
                              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
                            </svg>
                            Copy Server IP
                          </button>
                        </div>
                        <small class="field-help">IP address for PXE boot on this port</small>
                      </div>
                    </div>
                  {/if}
                  <div class="form-group">
                    <label>Description</label>
                    <input type="text" bind:value={port.description} placeholder="Optional description" />
                  </div>
                </div>
              {/each}
            </div>
          {/if}
        </div>
      </div>
      <div class="modal-footer">
        <button class="btn-secondary" on:click={closeModal}>Cancel</button>
        <button class="btn-primary" on:click={handleSubmit}>
          {editingServer ? 'Update' : 'Create'}
        </button>
      </div>
    </div>
  </div>
{/if}

<style>
  .servers-container {
    padding: 32px;
  }

  .servers-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 24px;
  }

  .servers-header h2 {
    margin: 0;
    font-size: 24px;
    font-weight: 700;
    color: var(--text-primary);
  }

  .btn-primary {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 10px 20px;
    background: var(--accent-color);
    color: white;
    border: none;
    border-radius: 8px;
    font-weight: 600;
    cursor: pointer;
    transition: transform 0.2s ease, box-shadow 0.2s ease;
  }

  .btn-primary:hover {
    background: var(--accent-dark);
    transform: translateY(-2px);
    box-shadow: var(--shadow-lg);
  }

  .btn-icon {
    width: 18px;
    height: 18px;
  }

  .loading, .error, .empty-state {
    text-align: center;
    padding: 48px;
    color: var(--text-secondary);
  }

  .error {
    color: #ef4444;
  }

  .servers-table {
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: 12px;
    overflow: hidden;
    box-shadow: var(--shadow-sm);
    transition: background-color 0.3s ease, border-color 0.3s ease;
  }

  table {
    width: 100%;
    border-collapse: collapse;
  }

  thead {
    background: var(--bg-tertiary);
  }

  th {
    padding: 16px;
    text-align: left;
    font-weight: 600;
    color: var(--text-primary);
    border-bottom: 2px solid var(--border-color);
  }

  td {
    padding: 16px;
    border-bottom: 1px solid var(--border-color);
    color: var(--text-primary);
  }

  tbody tr:hover {
    background: var(--bg-secondary);
  }

  .server-name-link {
    color: var(--accent-color);
    text-decoration: none;
    cursor: pointer;
    transition: color 0.2s ease;
  }

  .server-name-link:hover {
    color: #2563eb;
    text-decoration: underline;
  }

  .table-actions {
    display: flex;
    gap: 8px;
    align-items: center;
  }

  .power-control-buttons {
    display: flex;
    gap: 4px;
    margin-right: 8px;
  }

  .btn-power {
    padding: 6px 10px;
    border: none;
    border-radius: 6px;
    cursor: pointer;
    transition: all 0.2s ease;
    display: flex;
    align-items: center;
    justify-content: center;
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

  .power-state {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .power-state-badge {
    padding: 4px 12px;
    border-radius: 12px;
    font-size: 12px;
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
    color: var(--text-secondary);
  }

  .power-state-na {
    color: var(--text-secondary);
    font-size: 14px;
  }

  .btn-small {
    padding: 4px;
  }

  .btn-small svg {
    width: 14px;
    height: 14px;
  }

  .btn-icon-only {
    background: var(--bg-tertiary);
    border: 1px solid var(--border-color);
    padding: 6px;
    cursor: pointer;
    color: var(--text-primary);
    border-radius: 6px;
    transition: all 0.2s ease;
  }

  .btn-icon-only:hover {
    background: var(--bg-secondary);
    border-color: var(--accent-color);
    color: var(--accent-color);
    transform: translateY(-1px);
  }

  .btn-icon-only.btn-danger {
    border-color: var(--danger-color);
    color: var(--danger-color);
  }
  
  .btn-icon-only.btn-danger:hover {
    background: var(--danger-color);
    color: white;
    border-color: var(--danger-color);
  }

  .btn-icon-only svg {
    width: 18px;
    height: 18px;
  }

  .modal-overlay {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background: rgba(0, 0, 0, 0.5);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
    padding: 20px;
  }

  .modal-content {
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: 12px;
    width: 100%;
    max-width: 500px;
    max-height: 90vh;
    overflow-y: auto;
    box-shadow: var(--shadow-xl);
    color: var(--text-primary);
    transition: background-color 0.3s ease, border-color 0.3s ease;
  }

  .modal-content.large {
    max-width: 900px;
  }

  .modal-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 20px 24px;
    border-bottom: 1px solid var(--border-color);
    position: sticky;
    top: 0;
    background: var(--bg-primary);
    z-index: 10;
    transition: background-color 0.3s ease, border-color 0.3s ease;
  }

  .modal-header h3 {
    margin: 0;
    font-size: 20px;
    font-weight: 600;
  }

  .modal-body {
    padding: 24px;
  }

  .form-section {
    margin-bottom: 32px;
  }

  .form-section h4 {
    margin: 0 0 16px 0;
    font-size: 16px;
    font-weight: 600;
    color: var(--text-primary);
    padding-bottom: 8px;
    border-bottom: 2px solid var(--border-color);
  }

  .form-section h5 {
    margin: 16px 0 12px 0;
    font-size: 14px;
    font-weight: 600;
    color: var(--text-primary);
  }

  .section-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 16px;
  }

  .section-header h4 {
    margin: 0;
    border: none;
    padding: 0;
  }

  .form-error {
    background: #fee2e2;
    color: #991b1b;
    padding: 12px;
    border-radius: 8px;
    margin-bottom: 16px;
  }

  .form-row {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
  }

  .form-group {
    margin-bottom: 16px;
  }

  .form-group label {
    display: block;
    margin-bottom: 8px;
    font-weight: 600;
    color: var(--text-primary);
  }

  .form-group label .required {
    color: #ef4444;
  }

  .form-group input,
  .form-group textarea,
  .form-group select {
    width: 100%;
    padding: 10px 12px;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    font-size: 14px;
    background: var(--bg-primary);
    color: var(--text-primary);
    transition: border-color 0.2s ease, background-color 0.3s ease, color 0.3s ease;
    font-family: inherit;
  }

  .form-group input:focus,
  
  .form-group textarea:focus,
  
  .form-group select:focus {
  
    outline: none;
  
    border-color: var(--accent-color);
  
    box-shadow: 0 0 0 3px rgba(8, 145, 178, 0.1);
  
  }
  

  .form-group input[type="checkbox"] {
    width: auto;
  }

  .field-help {
    display: block;
    margin-top: 4px;
    font-size: 12px;
    color: var(--text-secondary);
  }

  .port-speed-input-group {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .port-speed-quick-buttons {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .btn-quick {
    padding: 6px 12px;
    background: var(--bg-tertiary);
    color: var(--text-primary);
    border-color: var(--border-color);
    border-radius: 6px;
    font-size: 13px;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s ease;
  }

  .btn-quick:hover {
    background: var(--bg-secondary);
    border-color: var(--accent-color);
    color: var(--accent-color);
  }

  .port-speed-display {
    font-size: 13px;
    color: var(--text-secondary);
    font-weight: 500;
  }

  .plugin-config {
    background: var(--bg-secondary);
    padding: 16px;
    border-radius: 8px;
    margin-top: 12px;
  }

  .plugin-test-section {
    margin-top: 16px;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  
  .test-controls {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 12px;
    flex-wrap: wrap;
  }
  
  .test-status {
    color: var(--text-secondary);
    font-size: 14px;
  }
  
  .test-success-indicator {
    display: flex;
    align-items: center;
    gap: 8px;
    color: #10b981;
    font-size: 14px;
    font-weight: 500;
  }
  
  .test-icon-small {
    width: 18px;
    height: 18px;
  }
  
  .capability-summary {
    display: flex;
    gap: 16px;
    margin: 12px 0;
    padding: 8px 0;
    border-top: 1px solid rgba(0, 0, 0, 0.1);
    border-bottom: 1px solid rgba(0, 0, 0, 0.1);
  }
  
  .capability-summary-item {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  
  .capability-label {
    font-size: 12px;
    color: var(--text-secondary);
    font-weight: 500;
  }
  
  .capability-value {
    font-size: 18px;
    font-weight: 600;
  }
  
  .capability-value.success {
    color: #10b981;
  }
  
  .capability-value.error {
    color: #ef4444;
  }
  
  .capability-list {
    margin-top: 12px;
  }
  
  .capability-badges {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-top: 8px;
  }
  
  .capability-badge {
    display: inline-block;
    padding: 4px 10px;
    background: #e0f2fe;
    color: #0369a1;
    border-radius: 12px;
    font-size: 12px;
    font-weight: 500;
  }
  
  .test-logs-details {
    margin-top: 12px;
    padding-top: 12px;
    border-top: 1px solid rgba(0, 0,0, 0.1);
  }
  
  .test-logs-details summary {
    cursor: pointer;
    font-weight: 500;
    color: var(--text-secondary);
    margin-bottom: 8px;
  }
  
  .test-logs-details summary:hover {
    color: var(--text-primary);
  }
  
  .test-logs-content {
    background: #1e293b;
    color: #e2e8f0;
    padding: 12px;
    border-radius: 6px;
    font-size: 12px;
    font-family: 'Courier New', monospace;
    overflow-x: auto;
    max-height: 200px;
    overflow-y: auto;
    white-space: pre-wrap;
    word-wrap: break-word;
  }

  .test-result {
    padding: 12px;
    border-radius: 8px;
    border: 2px solid;
  }

  .test-result.test-success {
    background: #f0fdf4;
    border-color: #22c55e;
    color: #166534;
  }

  .test-result.test-failure {
    background: #fef2f2;
    border-color: #ef4444;
    color: #991b1b;
  }

  .test-result-header {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 8px;
  }

  .test-icon {
    width: 20px;
    height: 20px;
  }

  .test-result-message {
    font-size: 14px;
    margin-bottom: 8px;
  }

  .test-result-details {
    margin-top: 8px;
    padding-top: 8px;
    border-top: 1px solid rgba(0, 0, 0, 0.1);
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .test-detail-item {
    display: flex;
    gap: 8px;
    font-size: 12px;
  }

  .test-detail-key {
    font-weight: 600;
  }

  .test-detail-value {
    color: var(--text-secondary);
  }

  .disks-list {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .disk-item {
    background: var(--bg-secondary);
    padding: 16px;
    border-radius: 8px;
    border-color: var(--border-color);
  }

  .disk-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 12px;
  }

  .empty-disks {
    color: var(--text-secondary);
    font-style: italic;
    margin: 0;
  }

  .btn-secondary {
    padding: 10px 20px;
    background: var(--bg-tertiary);
    color: var(--text-primary);
    border: none;
    border-radius: 8px;
    font-weight: 600;
    cursor: pointer;
    transition: background 0.2s ease;
  }

  .btn-secondary:hover {
    background: var(--accent-color);
  }

  .btn-secondary.btn-small {
    padding: 6px 12px;
    font-size: 14px;
  }

  .btn-secondary:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .modal-footer {
    display: flex;
    justify-content: flex-end;
    gap: 12px;
    padding: 20px 24px;
    border-top: 1px solid var(--border-color);
    position: sticky;
    bottom: 0;
    background: var(--bg-primary);
    transition: background-color 0.3s ease, border-color 0.3s ease;
  }

  .capabilities-cell {
    display: flex;
    flex-direction: column;
    gap: 8px;
    min-width: 200px;
  }

  .capabilities-badges {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }

  .capability-badge {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 4px 8px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 500;
    text-transform: capitalize;
  }

  .capability-badge.tested {
    background: #d1fae5;
    color: #065f46;
  }

  .capability-icon {
    width: 12px;
    height: 12px;
  }

  .test-logs-row {
    background: var(--bg-secondary);
  }

  .test-logs-container {
    padding: 16px;
  }

  .test-logs-container h4 {
    font-size: 13px;
    font-weight: 600;
    color: var(--text-secondary);
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin: 0 0 12px;
  }

  .test-logs-content {
    font-family: 'Courier New', monospace;
    font-size: 11px;
    line-height: 1.6;
    color: var(--text-primary);
    background: var(--bg-primary);
    padding: 12px;
    border-radius: 6px;
    border-color: var(--border-color);
    overflow-x: auto;
    white-space: pre-wrap;
    word-wrap: break-word;
    margin: 0;
    max-height: 300px;
    overflow-y: auto;
  }

  .btn-small {
    padding: 4px 8px;
    font-size: 11px;
  }

  .btn-text {
    background: none;
    border: none;
    color: var(--primary-color);
    cursor: pointer;
    padding: 4px 8px;
    font-size: 11px;
    text-decoration: underline;
  }

  .btn-text:hover {
    color: var(--primary-color-dark);
  }
</style>

