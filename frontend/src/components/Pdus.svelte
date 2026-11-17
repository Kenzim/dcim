<script>
  import PageHeader from './PageHeader.svelte';
  import {
    getPdus,
    getPduPlugins,
    createPdu,
    updatePdu,
    deletePdu,
    testPdu,
    getLocations,
    getRacks
  } from '../lib/api.js';
  import { formatWatts, formatAmps, formatRelativeTime, formatRackPosition } from '../lib/power.js';
  import { onMount } from 'svelte';

  const MAX_RACK_UNITS = 4;

  let pdus = [];
  let plugins = [];
  let locations = [];
  let racks = [];
  let availableRacks = [];
  let loading = true;
  let error = null;
  let showModal = false;
  let editingPdu = null;
  let formData = emptyForm();
  let formError = null;
  let pluginConfigError = null;
  let testingConnection = false;
  let testResult = null;
  let testedSignature = null;
  let submitting = false;

  function emptyForm() {
    return {
      name: '',
      description: '',
      location_id: null,
      rack_id: null,
      rack_unit: null,
      rack_units: 1,
      plugin_name: '',
      plugin_config: {},
      enabled: true,
      outlet_control_enabled: false
    };
  }

  onMount(async () => {
    await Promise.all([loadPdus(), loadPlugins(), loadLocations(), loadRacks()]);
  });

  async function loadPdus() {
    try {
      loading = true;
      error = null;
      pdus = await getPdus();
    } catch (err) {
      error = err.message;
      console.error('Failed to load PDUs:', err);
    } finally {
      loading = false;
    }
  }

  async function loadPlugins() {
    try {
      plugins = await getPduPlugins();
    } catch (err) {
      console.error('Failed to load PDU plugins:', err);
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
          clearRack();
        }
      }
    } else {
      availableRacks = [];
      clearRack();
    }
  }

  function clearRack() {
    formData.rack_id = null;
    formData.rack_unit = null;
    formData.rack_units = 1;
  }

  function onLocationChange() {
    updateAvailableRacks();
  }

  function getSelectedPlugin() {
    if (!formData.plugin_name) return null;
    return plugins.find(p => p.name === formData.plugin_name) || null;
  }

  function defaultConfig(plugin) {
    const config = {};
    const properties = plugin?.config_template?.properties || {};
    for (const [key, schema] of Object.entries(properties)) {
      if (schema.default !== undefined) {
        config[key] = schema.default;
      } else if (schema.type === 'boolean') {
        config[key] = false;
      } else if (schema.type === 'integer' || schema.type === 'number') {
        config[key] = null;
      } else {
        config[key] = '';
      }
    }
    return config;
  }

  function configSignature(data) {
    return JSON.stringify([data.plugin_name, data.plugin_config]);
  }

  function onPluginChange() {
    testResult = null;
    testedSignature = null;
    pluginConfigError = null;
    formData.plugin_config = defaultConfig(getSelectedPlugin());
  }

  function openAddModal() {
    editingPdu = null;
    formData = emptyForm();
    formError = null;
    pluginConfigError = null;
    testResult = null;
    testedSignature = null;
    updateAvailableRacks();
    showModal = true;
  }

  function openEditModal(pdu) {
    editingPdu = pdu;
    formData = {
      name: pdu.name,
      description: pdu.description || '',
      location_id: pdu.location_id,
      rack_id: pdu.rack_id || null,
      rack_unit: pdu.rack_unit ?? null,
      rack_units: pdu.rack_units ?? 1,
      plugin_name: pdu.plugin_name,
      plugin_config: { ...(pdu.plugin_config || {}) },
      enabled: pdu.enabled,
      outlet_control_enabled: pdu.outlet_control_enabled
    };
    formError = null;
    pluginConfigError = null;
    testResult = null;
    updateAvailableRacks();
    showModal = true;
  }

  function closeModal() {
    showModal = false;
    editingPdu = null;
    formError = null;
    pluginConfigError = null;
    testResult = null;
  }

  function missingRequiredFields(plugin) {
    const required = plugin?.config_template?.required || [];
    return required.filter(field => {
      const value = formData.plugin_config[field];
      return value === undefined || value === null || value === '';
    });
  }

  async function testConnection() {
    const plugin = getSelectedPlugin();
    if (!plugin) {
      pluginConfigError = 'Please select a plugin first';
      return;
    }

    const missing = missingRequiredFields(plugin);
    if (missing.length > 0) {
      pluginConfigError = `Missing required fields: ${missing.join(', ')}`;
      testResult = null;
      testedSignature = null;
      return;
    }

    try {
      testingConnection = true;
      pluginConfigError = null;
      testResult = null;
      const signature = configSignature(formData);
      const result = await testPdu(
        formData.plugin_name,
        preparedPluginConfig(plugin),
        editingPdu ? editingPdu.id : null
      );
      testResult = result;
      testedSignature = result.success === true ? signature : null;
      if (!result.success) {
        pluginConfigError = result.message || 'Connection test failed';
      }
    } catch (err) {
      testResult = { success: false, message: err.message || 'Connection test failed' };
      testedSignature = null;
      pluginConfigError = err.message || 'Connection test failed';
    } finally {
      testingConnection = false;
    }
  }

  function preparedPluginConfig(plugin) {
    const properties = plugin?.config_template?.properties || {};
    const config = {};
    for (const [key, value] of Object.entries(formData.plugin_config)) {
      const type = properties[key]?.type;
      if ((type === 'integer' || type === 'number') && value !== '' && value !== null) {
        config[key] = Number(value);
      } else {
        config[key] = value;
      }
    }
    return config;
  }

  function entriesOf(obj) {
    if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return [];
    return Object.entries(obj).filter(([, v]) => v === null || typeof v !== 'object');
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
    if (formData.rack_id && formData.rack_units > 0 && !formData.rack_unit) {
      formError = 'Rack unit is required when a rack is selected';
      return;
    }
    if (!formData.plugin_name) {
      formError = 'Plugin is required';
      return;
    }
    const plugin = getSelectedPlugin();
    const missing = missingRequiredFields(plugin);
    if (missing.length > 0) {
      formError = `Missing required plugin settings: ${missing.join(', ')}`;
      return;
    }
    if (!canSave) {
      formError = 'Please test the connection successfully before saving.';
      return;
    }

    try {
      formError = null;
      submitting = true;
      const payload = {
        name: formData.name.trim(),
        description: formData.description.trim() || null,
        location_id: Number(formData.location_id),
        rack_id: formData.rack_id ? Number(formData.rack_id) : null,
        rack_unit: formData.rack_id && formData.rack_units > 0 ? Number(formData.rack_unit) : null,
        rack_units: Number(formData.rack_units ?? 0),
        plugin_name: formData.plugin_name,
        plugin_config: preparedPluginConfig(plugin),
        enabled: formData.enabled,
        outlet_control_enabled: formData.outlet_control_enabled
      };

      if (editingPdu) {
        await updatePdu(editingPdu.id, payload);
      } else {
        const created = await createPdu(payload);
        if (created?.port_generation?.probe_error) {
          alert(`PDU saved, but probing its ports failed: ${created.port_generation.probe_error}`);
        }
      }
      closeModal();
      await loadPdus();
    } catch (err) {
      formError = err.message;
    } finally {
      submitting = false;
    }
  }

  async function handleDelete(pdu) {
    if (!confirm(`Are you sure you want to delete PDU "${pdu.name}"?`)) {
      return;
    }
    try {
      await deletePdu(pdu.id);
      await loadPdus();
    } catch (err) {
      alert('Failed to delete PDU: ' + err.message);
    }
  }

  function pluginLabel(pluginName) {
    return plugins.find(p => p.name === pluginName)?.display_name || pluginName || 'N/A';
  }

  $: selectedPlugin = formData.plugin_name ? plugins.find(p => p.name === formData.plugin_name) || null : null;
  $: selectedProperties = selectedPlugin?.config_template?.properties || {};
  $: selectedRequired = selectedPlugin?.config_template?.required || [];
  $: canSave = !!editingPdu || testedSignature === configSignature(formData);
  $: rackMaxUnits = (() => {
    if (!formData.rack_id) return MAX_RACK_UNITS;
    const rack = racks.find(r => r.id === Number(formData.rack_id));
    return rack ? Math.min(rack.units, MAX_RACK_UNITS) : MAX_RACK_UNITS;
  })();
</script>

<PageHeader title="PDUs">
  <svelte:fragment slot="actions">
    <button class="btn-primary" on:click={openAddModal}>
      <svg xmlns="http://www.w3.org/2000/svg" class="btn-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
      </svg>
      Add PDU
    </button>
  </svelte:fragment>
</PageHeader>

<div class="admin-page pdus-container">
  {#if loading}
    <div class="loading">Loading PDUs...</div>
  {:else if error}
    <div class="error">Error: {error}</div>
  {:else if pdus.length === 0}
    <div class="empty-state">
      <p>No PDUs found. Click "Add PDU" to create one.</p>
    </div>
  {:else}
    <div class="pdus-table admin-data-table">
      <table>
        <thead>
          <tr>
            <th>Name</th>
            <th>Location</th>
            <th>Rack</th>
            <th>Model</th>
            <th>Plugin</th>
            <th>Inlets / Outlets</th>
            <th>Power</th>
            <th>Current</th>
            <th>Last polled</th>
            <th>Status</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          {#each pdus as pdu}
            <tr>
              <td>
                <a href="/admin/pdus/{pdu.id}" class="pdu-name-link">
                  <strong>{pdu.name}</strong>
                </a>
              </td>
              <td>{locations.find(l => l.id === pdu.location_id)?.name || 'N/A'}</td>
              <td>{formatRackPosition(pdu)}</td>
              <td>{pdu.model || 'N/A'}</td>
              <td>{pluginLabel(pdu.plugin_name)}</td>
              <td>{pdu.inlet_count ?? 0} / {pdu.outlet_count ?? 0}</td>
              <td>{formatWatts(pdu.power_w)}</td>
              <td>{formatAmps(pdu.current_a)}</td>
              <td>{formatRelativeTime(pdu.last_polled_at)}</td>
              <td>
                <div class="status-stack">
                  <span class="status-badge" class:enabled={pdu.enabled} class:disabled={!pdu.enabled}>
                    {pdu.enabled ? 'Enabled' : 'Disabled'}
                  </span>
                  {#if pdu.last_poll_error}
                    <span class="status-badge poll-error" title={pdu.last_poll_error}>Poll error</span>
                  {/if}
                </div>
              </td>
              <td>
                <div class="table-actions">
                  <a href="/admin/pdus/{pdu.id}" class="btn-icon-only" title="View Details">
                    <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z" />
                    </svg>
                  </a>
                  <button class="btn-icon-only" on:click={() => openEditModal(pdu)} title="Edit">
                    <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
                    </svg>
                  </button>
                  <button class="btn-icon-only btn-danger" on:click={() => handleDelete(pdu)} title="Delete">
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
  <!-- svelte-ignore a11y-no-static-element-interactions -->
  <!-- svelte-ignore a11y-no-noninteractive-element-interactions -->
  <div
    class="modal-overlay"
    tabindex="-1"
    on:click={closeModal}
    on:keydown={(e) => e.key === 'Escape' && closeModal()}
  >
    <!-- svelte-ignore a11y-no-noninteractive-element-interactions -->
    <div class="modal-content" role="dialog" aria-modal="true" on:click|stopPropagation on:keydown|stopPropagation>
      <div class="modal-header">
        <h3>{editingPdu ? 'Edit PDU' : 'Add PDU'}</h3>
        <button class="btn-icon-only" on:click={closeModal} title="Close">
          <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
          </svg>
        </button>
      </div>

      <form on:submit|preventDefault={handleSubmit}>
        <div class="modal-body">
          {#if formError}
            <div class="form-error">{formError}</div>
          {/if}

          <div class="form-section">
            <h4>Details</h4>
            <div class="form-group">
              <label for="pdu-name">Name <span class="required">*</span></label>
              <input id="pdu-name" type="text" bind:value={formData.name} required />
            </div>
            <div class="form-group">
              <label for="pdu-description">Description</label>
              <textarea id="pdu-description" rows="2" bind:value={formData.description}></textarea>
            </div>
            <div class="form-row">
              <div class="form-group">
                <label for="pdu-location">Location <span class="required">*</span></label>
                <select id="pdu-location" bind:value={formData.location_id} on:change={onLocationChange} required>
                  <option value={null}>Select a location</option>
                  {#each locations as location}
                    <option value={location.id}>{location.name}</option>
                  {/each}
                </select>
              </div>
              <div class="form-group">
                <label for="pdu-rack">Rack</label>
                <select id="pdu-rack" bind:value={formData.rack_id} disabled={!formData.location_id}>
                  <option value={null}>No rack</option>
                  {#each availableRacks as rack}
                    <option value={rack.id}>{rack.name}</option>
                  {/each}
                </select>
              </div>
            </div>
            <div class="form-row">
              <div class="form-group">
                <label for="pdu-rack-unit">Rack unit</label>
                <input
                  id="pdu-rack-unit"
                  type="number"
                  min="1"
                  max={rackMaxUnits}
                  bind:value={formData.rack_unit}
                  disabled={!formData.rack_id || formData.rack_units === 0}
                />
              </div>
              <div class="form-group">
                <label for="pdu-height">Height (U)</label>
                <input id="pdu-height" type="number" min="0" max={MAX_RACK_UNITS} bind:value={formData.rack_units} />
                <small class="field-help">0 means zero-U (vertical, not drawn in the rack elevation). Up to {MAX_RACK_UNITS} U.</small>
              </div>
            </div>
            <div class="form-group">
              <label class="checkbox-label">
                <input type="checkbox" bind:checked={formData.enabled} />
                Enabled
              </label>
            </div>
          </div>

          <div class="form-section">
            <h4>Plugin</h4>
            <div class="form-group">
              <label for="pdu-plugin">Plugin <span class="required">*</span></label>
              <select id="pdu-plugin" bind:value={formData.plugin_name} on:change={onPluginChange} disabled={!!editingPdu} required>
                <option value="">Select a plugin</option>
                {#each plugins as plugin}
                  <option value={plugin.name}>{plugin.display_name || plugin.name}</option>
                {/each}
              </select>
              {#if editingPdu}
                <small class="field-help">The plugin cannot be changed after the PDU is created.</small>
              {/if}
            </div>

            {#if selectedPlugin && Object.keys(selectedProperties).length > 0}
              <div class="plugin-config">
                {#each Object.entries(selectedProperties) as [key, schema]}
                  <div class="form-group">
                    <label for="pdu-cfg-{key}">
                      {schema.title || key}
                      {#if selectedRequired.includes(key)}<span class="required">*</span>{/if}
                    </label>
                    {#if schema.type === 'boolean'}
                      <input id="pdu-cfg-{key}" type="checkbox" bind:checked={formData.plugin_config[key]} />
                    {:else if schema.enum}
                      <select id="pdu-cfg-{key}" bind:value={formData.plugin_config[key]}>
                        {#each schema.enum as option}
                          <option value={option}>{option}</option>
                        {/each}
                      </select>
                    {:else if schema.type === 'integer' || schema.type === 'number'}
                      <input id="pdu-cfg-{key}" type="number" min={schema.minimum ?? undefined} max={schema.maximum ?? undefined} bind:value={formData.plugin_config[key]} />
                    {:else if schema.format === 'password'}
                      <input id="pdu-cfg-{key}" type="password" autocomplete="new-password" bind:value={formData.plugin_config[key]} />
                    {:else}
                      <input id="pdu-cfg-{key}" type="text" bind:value={formData.plugin_config[key]} />
                    {/if}
                    {#if schema.description}
                      <small class="field-help">{schema.description}</small>
                    {/if}
                  </div>
                {/each}
              </div>
            {/if}

            <div class="plugin-test-section">
              <div class="test-controls">
                <button type="button" class="btn-secondary" on:click={testConnection} disabled={testingConnection || !formData.plugin_name}>
                  {testingConnection ? 'Testing...' : 'Test connection'}
                </button>
              </div>

              {#if pluginConfigError}
                <div class="form-error">{pluginConfigError}</div>
              {/if}

              {#if testResult}
                <div class="test-result" class:test-success={testResult.success} class:test-failure={!testResult.success}>
                  <div class="test-result-header">
                    <strong>{testResult.success ? 'Connection successful' : 'Connection failed'}</strong>
                  </div>
                  {#if testResult.message}
                    <div class="test-result-message">{testResult.message}</div>
                  {/if}
                  {#if testResult.pdu_info}
                    <div class="test-result-details">
                      {#each entriesOf(testResult.pdu_info) as [key, value]}
                        <div class="test-detail-item">
                          <span class="test-detail-key">{key}:</span>
                          <span class="test-detail-value">{value}</span>
                        </div>
                      {/each}
                    </div>
                  {/if}
                </div>
              {/if}

              {#if !canSave}
                <small class="field-help">Please test the connection successfully to enable saving.</small>
              {/if}
            </div>
          </div>

          <div class="form-section">
            <h4>Outlet control</h4>
            <div class="control-warning">
              Enabling this lets Rackflow switch power to outlets on this PDU (on, off and cycle), which can shut down the servers
              connected to them. Leave it off unless the PDU is switched and you want Rackflow to control it.
            </div>
            <div class="form-group">
              <label class="checkbox-label">
                <input type="checkbox" bind:checked={formData.outlet_control_enabled} />
                Allow outlet control from Rackflow
              </label>
            </div>
          </div>
        </div>

        <div class="modal-footer">
          <button type="button" class="btn-secondary" on:click={closeModal}>Cancel</button>
          <button type="submit" class="btn-primary" disabled={submitting || !canSave}>
            {submitting ? 'Saving...' : editingPdu ? 'Update PDU' : 'Create PDU'}
          </button>
        </div>
      </form>
    </div>
  </div>
{/if}

<style>
  .btn-icon {
    width: 15px;
    height: 15px;
  }

  .loading, .error, .empty-state {
    text-align: center;
    padding: 48px;
    color: var(--text-secondary);
  }

  .error {
    color: var(--danger-color);
  }

  .pdus-table table {
    min-width: 960px;
  }

  .status-stack {
    display: flex;
    flex-direction: column;
    gap: 4px;
    align-items: flex-start;
  }

  .status-badge.enabled {
    background: var(--success-bg);
    color: var(--success-text);
  }

  .status-badge.disabled {
    background: var(--danger-bg);
    color: var(--danger-text);
  }

  .status-badge.poll-error {
    background: var(--danger-bg);
    color: var(--danger-text);
  }

  .table-actions {
    display: flex;
    gap: 4px;
    align-items: center;
  }

  .btn-icon-only {
    background: transparent;
    border: 1px solid transparent;
    padding: 3px;
    cursor: pointer;
    color: var(--text-tertiary);
    border-radius: 3px;
    transition: background 0.12s ease, color 0.12s ease, border-color 0.12s ease;
  }

  .btn-icon-only:hover {
    background: var(--bg-tertiary);
    border-color: var(--border-color);
    color: var(--accent-color);
  }

  .btn-icon-only.btn-danger:hover {
    background: var(--danger-bg);
    color: var(--danger-color);
    border-color: transparent;
  }

  .btn-icon-only svg {
    width: 15px;
    height: 15px;
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
    max-width: 640px;
    max-height: 90vh;
    overflow-y: auto;
    box-shadow: var(--shadow-xl);
    color: var(--text-primary);
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

  .form-error {
    background: var(--danger-bg);
    color: var(--danger-text);
    padding: 12px;
    border-radius: 8px;
    margin-bottom: 16px;
  }

  .control-warning {
    background: var(--warning-bg, var(--bg-secondary));
    color: var(--warning-text, var(--text-primary));
    border: 1px solid var(--warning-color, var(--border-color));
    padding: 12px;
    border-radius: 8px;
    margin-bottom: 16px;
    font-size: 13px;
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

  .form-group label.checkbox-label {
    display: flex;
    align-items: center;
    gap: 8px;
    font-weight: 500;
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

  .form-group input:disabled,
  .form-group select:disabled {
    background: var(--bg-secondary);
    color: var(--text-secondary);
    cursor: not-allowed;
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
  }

  .pdu-name-link {
    color: var(--accent-color);
    text-decoration: none;
    font-weight: 600;
  }

  .pdu-name-link:hover {
    color: var(--accent-dark);
    text-decoration: underline;
  }
</style>
