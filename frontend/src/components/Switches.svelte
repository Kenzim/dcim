<script>
  import PageHeader from './PageHeader.svelte';
  import { getSwitches, createSwitch, updateSwitch, deleteSwitch, getSwitchPlugins, getLocations, getRacks, testSwitchConnection } from '../lib/api.js';
  import { onMount } from 'svelte';
  import { link } from 'svelte-spa-router';

  let switches = [];
  let plugins = [];
  let locations = [];
  let racks = [];
  let availableRacks = [];
  let loading = true;
  let error = null;
  let showModal = false;
  let editingSwitch = null;
  let formData = {
    name: '',
    description: '',
    location_id: null,
    rack_id: null,
    rack_unit: null,
    rack_units: 1,
    plugin_name: '',
    plugin_config: {},
    enabled: true,
    port_count: null,
    model: '',
    serial_number: '',
    firmware_version: ''
  };
  let formError = null;
  let pluginConfigError = null;
  let testingConnection = false;
  let testResult = null;
  let testPassed = false;
  let submitting = false;

  onMount(async () => {
    await Promise.all([loadSwitches(), loadPlugins(), loadLocations(), loadRacks()]);
  });

  async function loadSwitches() {
    try {
      loading = true;
      error = null;
      switches = await getSwitches();
    } catch (err) {
      error = err.message;
      console.error('Failed to load switches:', err);
    } finally {
      loading = false;
    }
  }

  async function loadPlugins() {
    try {
      plugins = await getSwitchPlugins();
    } catch (err) {
      console.error('Failed to load switch plugins:', err);
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
      const locationId = Number(formData.location_id);
      availableRacks = racks.filter(r => Number(r.location_id) === locationId);
      if (formData.rack_id) {
        const currentRack = racks.find(r => r.id === formData.rack_id);
        if (!currentRack || Number(currentRack.location_id) !== locationId) {
          formData.rack_id = null;
          formData.rack_unit = null;
          formData.rack_units = 1;
        }
      }
    } else {
      availableRacks = [];
      formData.rack_id = null;
      formData.rack_unit = null;
      formData.rack_units = 1;
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

  $: if (formData.location_id && racks.length > 0) {
    updateAvailableRacks();
  }

  function openAddModal() {
    editingSwitch = null;
    previousPluginName = null;
    formData = {
      name: '',
      description: '',
      location_id: null,
      rack_id: null,
      rack_unit: null,
      rack_units: 1,
      plugin_name: '',
      plugin_config: {},
      enabled: true,
      port_count: null,
      model: '',
      serial_number: '',
      firmware_version: ''
    };
    formError = null;
    pluginConfigError = null;
    testResult = null;
    testPassed = false;
    showModal = true;
  }

  function openEditModal(switchItem) {
    editingSwitch = switchItem;
    previousPluginName = switchItem.plugin_name;
    formData = {
      name: switchItem.name,
      description: switchItem.description || '',
      location_id: switchItem.location_id,
      rack_id: switchItem.rack_id || null,
      rack_unit: switchItem.rack_unit || null,
      rack_units: switchItem.rack_units ?? 1,
      plugin_name: switchItem.plugin_name,
      plugin_config: switchItem.plugin_config || {},
      enabled: switchItem.enabled,
      port_count: switchItem.port_count,
      model: switchItem.model || '',
      serial_number: switchItem.serial_number || '',
      firmware_version: switchItem.firmware_version || ''
    };
    formError = null;
    pluginConfigError = null;
    testResult = null;
    // For editing, allow save without connection test (connection was already tested when created)
    testPassed = true; // Allow editing existing switches without re-testing
    updateAvailableRacks();
    showModal = true;
  }

  function closeModal() {
    showModal = false;
    editingSwitch = null;
    formError = null;
    pluginConfigError = null;
  }

  function getSelectedPlugin() {
    if (!formData.plugin_name) return null;
    return plugins.find(p => p.name === formData.plugin_name);
  }

  let previousPluginName = null;

  function updatePluginConfig() {
    const plugin = getSelectedPlugin();
    
    if (previousPluginName !== null && previousPluginName !== formData.plugin_name) {
      formData.plugin_config = {};
      testResult = null;
      testPassed = false;
      pluginConfigError = null;
      // Clear hardware info when plugin changes
      formData.model = '';
      formData.serial_number = '';
      formData.firmware_version = '';
      formData.port_count = null;
    }
    previousPluginName = formData.plugin_name;
    
    if (!plugin) {
      formData.plugin_config = {};
      return;
    }
    
    if (!plugin.config_template) {
      formData.plugin_config = {};
      return;
    }

    const existingConfig = formData.plugin_config || {};
    const config = {};
    const properties = plugin.config_template.properties || {};
    
    for (const [key, schema] of Object.entries(properties)) {
      if (existingConfig.hasOwnProperty(key)) {
        config[key] = existingConfig[key];
      } else if (schema.type === 'boolean') {
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
    testResult = null;
    testPassed = false;
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
      
      const result = await testSwitchConnection(formData.plugin_name, formData.plugin_config);
      testResult = result;
      testPassed = result.success === true;
      
      if (!testPassed) {
        pluginConfigError = result.message || 'Connection test failed';
        // Clear hardware info on failure
        formData.model = '';
        formData.serial_number = '';
        formData.firmware_version = '';
        formData.port_count = null;
      } else {
        // Auto-populate hardware info from switch_info if available
        if (result.switch_info) {
          if (result.switch_info.model) {
            formData.model = result.switch_info.model;
          }
          if (result.switch_info.serial_number) {
            formData.serial_number = result.switch_info.serial_number;
          }
          if (result.switch_info.firmware_version) {
            formData.firmware_version = result.switch_info.firmware_version;
          }
          if (result.switch_info.port_count) {
            formData.port_count = result.switch_info.port_count;
          }
        }
      }
    } catch (err) {
      testResult = {
        success: false,
        message: err.message || 'Connection test failed'
      };
      testPassed = false;
      pluginConfigError = err.message || 'Connection test failed';
      // Clear hardware info on error
      formData.model = '';
      formData.serial_number = '';
      formData.firmware_version = '';
      formData.port_count = null;
    } finally {
      testingConnection = false;
    }
  }

  async function handleSubmit() {
    if (!formData.name.trim()) {
      formError = 'Name is required';
      return;
    }
    if (!formData.location_id) {
      formError = 'Location is required';
      return;
    }
    if (!formData.plugin_name) {
      formError = 'Plugin is required';
      return;
    }
    if (!formData.plugin_config || Object.keys(formData.plugin_config).length === 0) {
      formError = 'Plugin configuration is required';
      return;
    }
    // Require successful connection test before saving (only for new switches)
    if (!editingSwitch && !testPassed) {
      formError = 'Please test the connection successfully before saving. The connection test will automatically populate switch information.';
      return;
    }

    try {
      formError = null;
      submitting = true;
      const submitData = {
        ...formData,
        port_count: formData.port_count ? parseInt(formData.port_count) : null,
        model: formData.model.trim() || null,
        serial_number: formData.serial_number.trim() || null,
        firmware_version: formData.firmware_version.trim() || null
      };

      if (editingSwitch) {
        await updateSwitch(editingSwitch.id, submitData);
      } else {
        await createSwitch(submitData);
      }
      closeModal();
      await loadSwitches();
    } catch (err) {
      formError = err.message;
    } finally {
      submitting = false;
    }
  }

  async function handleDelete(switchItem) {
    if (!confirm(`Are you sure you want to delete switch "${switchItem.name}"?`)) {
      return;
    }

    try {
      await deleteSwitch(switchItem.id);
      await loadSwitches();
    } catch (err) {
      alert('Failed to delete switch: ' + err.message);
    }
  }
</script>

<PageHeader title="Network Switches" />

<div class="switches-container">
  <div class="switches-header">
    <h2>Network Switches</h2>
    <button class="btn-primary" on:click={openAddModal}>
      <svg xmlns="http://www.w3.org/2000/svg" class="btn-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
      </svg>
      Add Switch
    </button>
  </div>

  {#if loading}
    <div class="loading">Loading switches...</div>
  {:else if error}
    <div class="error">Error: {error}</div>
  {:else if switches.length === 0}
    <div class="empty-state">
      <p>No switches found. Click "Add Switch" to create one.</p>
    </div>
  {:else}
    <div class="switches-table">
      <table>
        <thead>
          <tr>
            <th>Name</th>
            <th>Model</th>
            <th>Ports</th>
            <th>Location</th>
            <th>Rack</th>
            <th>Plugin</th>
            <th>Status</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          {#each switches as switchItem}
            <tr>
              <td>
                <a href="/admin/switches/{switchItem.id}" class="switch-name-link">
                  <strong>{switchItem.name}</strong>
                </a>
              </td>
              <td>{switchItem.model || 'N/A'}</td>
              <td>{switchItem.port_count || 'N/A'}</td>
              <td>{switchItem.location_id ? (locations.find(l => l.id === switchItem.location_id)?.name || 'N/A') : 'N/A'}</td>
              <td>
                {#if switchItem.rack_id}
                  {racks.find(r => r.id === switchItem.rack_id)?.name || 'N/A'}
                  {#if switchItem.rack_unit != null}
                    {(switchItem.rack_units ?? 1) > 1 ? `(U${switchItem.rack_unit}–${switchItem.rack_unit + (switchItem.rack_units ?? 1) - 1})` : `(U${switchItem.rack_unit})`}
                  {/if}
                {:else}
                  N/A
                {/if}
              </td>
              <td>{switchItem.plugin_name || 'N/A'}</td>
              <td>
                <span class="status-badge" class:enabled={switchItem.enabled} class:disabled={!switchItem.enabled}>
                  {switchItem.enabled ? 'Enabled' : 'Disabled'}
                </span>
              </td>
              <td>
                <div class="table-actions">
                  <a href="/admin/switches/{switchItem.id}" class="btn-icon-only" title="View Details">
                    <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z" />
                    </svg>
                  </a>
                  <button class="btn-icon-only" on:click={() => openEditModal(switchItem)} title="Edit">
                    <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
                    </svg>
                  </button>
                  <button class="btn-icon-only btn-danger" on:click={() => handleDelete(switchItem)} title="Delete">
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
        <h3>{editingSwitch ? 'Edit Switch' : 'Add Switch'}</h3>
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
              <label for="switch-name">Name *</label>
              <input id="switch-name" type="text" bind:value={formData.name} required />
            </div>
          </div>
          <div class="form-group">
            <label for="switch-description">Description</label>
            <textarea id="switch-description" bind:value={formData.description} rows="2"></textarea>
          </div>
        </div>

        <div class="form-section">
          <h4>Hardware Information</h4>
          <p class="field-help" style="margin-bottom: 16px; color: var(--text-secondary);">
            Hardware information will be automatically populated after a successful connection test.
          </p>
          <div class="form-row">
            <div class="form-group">
              <label for="switch-model">Model</label>
              <input 
                id="switch-model" 
                type="text" 
                bind:value={formData.model} 
                placeholder="Auto-filled after connection test" 
                disabled={!testPassed}
                readonly={testPassed}
              />
            </div>
            <div class="form-group">
              <label for="port-count">Port Count</label>
              <input 
                id="port-count" 
                type="number" 
                bind:value={formData.port_count} 
                min="1" 
                placeholder="Auto-filled after connection test" 
                disabled={!testPassed}
                readonly={testPassed}
              />
            </div>
          </div>
          <div class="form-row">
            <div class="form-group">
              <label for="serial-number">Serial Number</label>
              <input 
                id="serial-number" 
                type="text" 
                bind:value={formData.serial_number} 
                placeholder="Auto-filled after connection test" 
                disabled={!testPassed}
                readonly={testPassed}
              />
            </div>
            <div class="form-group">
              <label for="firmware-version">Firmware Version</label>
              <input 
                id="firmware-version" 
                type="text" 
                bind:value={formData.firmware_version} 
                placeholder="Auto-filled after connection test" 
                disabled={!testPassed}
                readonly={testPassed}
              />
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
              <label for="rack-unit">Bottom U</label>
              <input 
                id="rack-unit" 
                type="number" 
                bind:value={formData.rack_unit} 
                min="1" 
                max={getMaxRackUnits()}
                disabled={!formData.rack_id}
                placeholder="1-{getMaxRackUnits()}"
              />
              {#if formData.rack_id && formData.rack_unit != null && formData.rack_units}
                {@const lo = formData.rack_unit}
                {@const hi = formData.rack_unit + formData.rack_units - 1}
                <small class="field-help">Occupies U{lo}–{hi}</small>
              {/if}
            </div>
            <div class="form-group">
              <label for="rack-units">Size (U)</label>
              <input 
                id="rack-units" 
                type="number" 
                bind:value={formData.rack_units} 
                min="1" 
                max={Math.min(getMaxRackUnits() || 10, 10)}
                disabled={!formData.rack_id}
              />
              {#if formData.rack_id}
                <small class="field-help">Height in rack units (1–{Math.min(getMaxRackUnits() || 10, 10)})</small>
              {/if}
            </div>
          </div>
        </div>

        <div class="form-section">
          <h4>Management Plugin</h4>
          <div class="form-group">
            <label for="plugin">Plugin *</label>
            <select id="plugin" bind:value={formData.plugin_name} on:change={updatePluginConfig}>
              <option value="">Select a plugin</option>
              {#each plugins as plugin}
                <option value={plugin.name}>{plugin.name} (v{plugin.version})</option>
              {/each}
            </select>
          </div>

          {#if formData.plugin_name}
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
                    <button type="button" class="btn-secondary" on:click={testConnection} disabled={testingConnection || !formData.plugin_name}>
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
                </div>
              </div>
            {/if}
          {/if}
        </div>

        <div class="form-section">
          <h4>Status</h4>
          <div class="form-group">
            <label style="display: flex; align-items: center; gap: 8px; cursor: pointer;">
              <input type="checkbox" bind:checked={formData.enabled} style="cursor: pointer;" />
              <span>Enabled</span>
            </label>
            <small class="field-help">Disable to temporarily exclude this switch from operations</small>
          </div>
        </div>
      </div>
      <div class="modal-footer">
        <button class="btn-secondary" on:click={closeModal}>Cancel</button>
        <button class="btn-primary" on:click={handleSubmit} disabled={(!editingSwitch && !testPassed) || submitting}>
          {#if submitting}
            {editingSwitch ? 'Updating...' : 'Creating...'}
          {:else}
            {editingSwitch ? 'Update' : 'Create'}
          {/if}
        </button>
        {#if !editingSwitch && !testPassed}
          <small style="color: var(--text-secondary); font-size: 12px; margin-top: 8px; display: block;">
            Please test the connection successfully to enable saving.
          </small>
        {/if}
      </div>
    </div>
  </div>
{/if}

<style>
  .switches-container {
    padding: 32px;
  }

  .switches-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 24px;
  }

  .switches-header h2 {
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
    color: var(--danger-color);
  }

  .switches-table {
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

  .status-badge {
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

  .table-actions {
    display: flex;
    gap: 8px;
    align-items: center;
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
    background: var(--overlay-bg);
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

  .form-error {
    background: var(--danger-bg);
    color: var(--danger-text);
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
    color: var(--danger-color);
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
    box-shadow: var(--focus-ring-accent);
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

  .test-result {
    padding: 12px;
    border-radius: 8px;
    border: 2px solid;
  }

  .test-result.test-success {
    background: var(--success-bg);
    border-color: var(--success-color);
    color: var(--success-text);
  }

  .test-result.test-failure {
    background: var(--danger-bg);
    border-color: var(--danger-color);
    color: var(--danger-text);
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
    border-top: 1px solid var(--border-color);
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

  .btn-secondary:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .btn-primary:disabled {
    opacity: 0.5;
    cursor: not-allowed;
    background: var(--bg-tertiary);
    color: var(--text-secondary);
  }

  .btn-primary:disabled:hover {
    transform: none;
    box-shadow: none;
  }

  input:disabled,
  input[readonly] {
    background: var(--bg-secondary);
    color: var(--text-secondary);
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

  .switch-name-link {
    color: var(--accent-color);
    text-decoration: none;
    font-weight: 600;
    transition: color 0.2s;
  }

  .switch-name-link:hover {
    color: var(--accent-dark);
    text-decoration: underline;
  }
</style>
