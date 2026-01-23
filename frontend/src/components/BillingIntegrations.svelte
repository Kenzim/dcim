<script>
  import PageHeader from './PageHeader.svelte';
  import { 
    getBillingIntegrations, 
    createBillingIntegration, 
    updateBillingIntegration, 
    deleteBillingIntegration,
    rotateBillingIntegrationKey,
    getBillingIntegrationTypes
  } from '../lib/api.js';
  import { onMount } from 'svelte';

  let integrations = [];
  let integrationTypes = [];
  let loading = true;
  let error = null;
  let showModal = false;
  let editingIntegration = null;
  let formData = { 
    name: '', 
    integration_type: '',
    description: '',
    enabled: true,
    config: {}
  };
  let formError = null;
  let showingApiKey = {};
  let rotatingKey = {};

  onMount(async () => {
    await Promise.all([loadIntegrations(), loadIntegrationTypes()]);
  });

  async function loadIntegrations() {
    try {
      loading = true;
      error = null;
      integrations = await getBillingIntegrations();
    } catch (err) {
      error = err.message;
      console.error('Failed to load integrations:', err);
    } finally {
      loading = false;
    }
  }

  async function loadIntegrationTypes() {
    try {
      integrationTypes = await getBillingIntegrationTypes();
    } catch (err) {
      console.error('Failed to load integration types:', err);
    }
  }

  function openAddModal() {
    editingIntegration = null;
    // Default to first available integration type (usually 'whmcs')
    const defaultType = integrationTypes.length > 0 ? integrationTypes[0].type : '';
    formData = { 
      name: '', 
      integration_type: defaultType,
      description: '',
      enabled: true,
      config: {}
    };
    formError = null;
    showModal = true;
  }

  function openEditModal(integration) {
    editingIntegration = integration;
    formData = { 
      name: integration.name,
      integration_type: integration.integration_type,
      description: integration.description || '',
      enabled: integration.enabled,
      config: integration.config || {}
    };
    formError = null;
    showModal = true;
  }

  function closeModal() {
    showModal = false;
    editingIntegration = null;
    const defaultType = integrationTypes.length > 0 ? integrationTypes[0].type : '';
    formData = { 
      name: '', 
      integration_type: defaultType,
      description: '',
      enabled: true,
      config: {}
    };
    formError = null;
  }

  async function handleSubmit() {
    if (!formData.name.trim()) {
      formError = 'Name is required';
      return;
    }

    try {
      formError = null;
      if (editingIntegration) {
        await updateBillingIntegration(editingIntegration.id, formData);
      } else {
        await createBillingIntegration(formData);
      }
      closeModal();
      await loadIntegrations();
    } catch (err) {
      formError = err.message;
    }
  }

  async function handleDelete(integration) {
    if (!confirm(`Are you sure you want to delete integration "${integration.name}"?`)) {
      return;
    }

    try {
      await deleteBillingIntegration(integration.id);
      await loadIntegrations();
    } catch (err) {
      alert('Failed to delete integration: ' + err.message);
    }
  }

  function toggleApiKey(integrationId) {
    showingApiKey[integrationId] = !showingApiKey[integrationId];
    showingApiKey = showingApiKey; // Trigger reactivity
  }

  async function copyApiKey(apiKey) {
    try {
      await navigator.clipboard.writeText(apiKey);
      alert('API key copied to clipboard!');
    } catch (err) {
      alert('Failed to copy API key: ' + err.message);
    }
  }

  async function handleRotateKey(integration) {
    if (!confirm(`Are you sure you want to rotate the API key for "${integration.name}"? The old key will stop working immediately.`)) {
      return;
    }

    try {
      rotatingKey[integration.id] = true;
      const updated = await rotateBillingIntegrationKey(integration.id);
      await loadIntegrations();
      alert('API key rotated successfully! New key: ' + updated.api_key);
    } catch (err) {
      alert('Failed to rotate API key: ' + err.message);
    } finally {
      rotatingKey[integration.id] = false;
      rotatingKey = rotatingKey; // Trigger reactivity
    }
  }
</script>

<PageHeader title="Billing Integrations" />

<div class="integrations-container">
  <div class="integrations-header">
    <h2>Billing Integrations</h2>
    <button class="btn-primary" on:click={openAddModal}>
      <svg xmlns="http://www.w3.org/2000/svg" class="btn-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
      </svg>
      Add Integration
    </button>
  </div>

  {#if loading}
    <div class="loading">Loading integrations...</div>
  {:else if error}
    <div class="error">Error: {error}</div>
  {:else if integrations.length === 0}
    <div class="empty-state">
      <p>No integrations found. Click "Add Integration" to create one.</p>
    </div>
  {:else}
    <div class="integrations-grid">
      {#each integrations as integration}
        <div class="integration-card">
          <div class="integration-header">
            <div>
              <h3>{integration.name}</h3>
              <span class="integration-type">{integration.integration_type}</span>
            </div>
            <div class="integration-status">
              {#if integration.enabled}
                <span class="status-badge status-active">Active</span>
              {:else}
                <span class="status-badge status-inactive">Disabled</span>
              {/if}
            </div>
          </div>
          
          {#if integration.description}
            <p class="integration-description">{integration.description}</p>
          {/if}

          <div class="integration-details">
            <div class="detail-item">
              <span class="detail-label">API Key:</span>
              <div class="api-key-container">
                {#if showingApiKey[integration.id]}
                  <code class="api-key">{integration.api_key}</code>
                  <button class="btn-icon-small" on:click={() => copyApiKey(integration.api_key)} title="Copy">
                    <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
                    </svg>
                  </button>
                {:else}
                  <span class="api-key-hidden">••••••••••••••••</span>
                {/if}
                <button class="btn-icon-small" on:click={() => toggleApiKey(integration.id)} title={showingApiKey[integration.id] ? 'Hide' : 'Show'}>
                  <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    {#if showingApiKey[integration.id]}
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13.875 18.825A10.05 10.05 0 0112 19c-4.478 0-8.268-2.943-9.543-7a9.97 9.97 0 011.563-3.029m5.858.908a3 3 0 114.243 4.243M9.878 9.878l4.242 4.242M9.88 9.88l-3.29-3.29m7.532 7.532l3.29 3.29M3 3l3.59 3.59m0 0A9.953 9.953 0 0112 5c4.478 0 8.268 2.943 9.543 7a10.025 10.025 0 01-4.367 5.127m0 0L21 21" />
                    {:else}
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z" />
                    {/if}
                  </svg>
                </button>
              </div>
            </div>
            
            {#if integration.last_used_at}
              <div class="detail-item">
                <span class="detail-label">Last Used:</span>
                <span>{new Date(integration.last_used_at).toLocaleString()}</span>
              </div>
            {/if}
            
            {#if integration.last_used_ip}
              <div class="detail-item">
                <span class="detail-label">Last IP:</span>
                <span>{integration.last_used_ip}</span>
              </div>
            {/if}
          </div>

          <div class="integration-actions">
            <button class="btn-secondary btn-small" on:click={() => handleRotateKey(integration)} disabled={rotatingKey[integration.id]}>
              {rotatingKey[integration.id] ? 'Rotating...' : 'Rotate Key'}
            </button>
            <button class="btn-icon-only" on:click={() => openEditModal(integration)} title="Edit">
              <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
              </svg>
            </button>
            <button class="btn-icon-only btn-danger" on:click={() => handleDelete(integration)} title="Delete">
              <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
              </svg>
            </button>
          </div>
        </div>
      {/each}
    </div>
  {/if}
</div>

{#if showModal}
  <div class="modal-overlay" on:click={closeModal}>
    <div class="modal-content" on:click|stopPropagation>
      <div class="modal-header">
        <h3>{editingIntegration ? 'Edit Integration' : 'Add Integration'}</h3>
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
        <div class="form-group">
          <label for="integration-name">Name *</label>
          <input
            id="integration-name"
            type="text"
            bind:value={formData.name}
            placeholder="e.g., WHMCS Production"
            required
          />
        </div>
        <div class="form-group">
          <label for="integration-type">Integration Type *</label>
          <select id="integration-type" bind:value={formData.integration_type}>
            {#each integrationTypes as type}
              <option value={type.type}>{type.name}</option>
            {/each}
          </select>
        </div>
        <div class="form-group">
          <label for="integration-description">Description</label>
          <textarea
            id="integration-description"
            bind:value={formData.description}
            placeholder="Optional description"
            rows="3"
          ></textarea>
        </div>
        <div class="form-group">
          <label>
            <input type="checkbox" bind:checked={formData.enabled} />
            Enabled
          </label>
        </div>
      </div>
      <div class="modal-footer">
        <button class="btn-secondary" on:click={closeModal}>Cancel</button>
        <button class="btn-primary" on:click={handleSubmit}>
          {editingIntegration ? 'Update' : 'Create'}
        </button>
      </div>
    </div>
  </div>
{/if}

<style>
  .integrations-container {
    padding: 32px;
  }

  .integrations-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 24px;
  }

  .integrations-header h2 {
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
    transform: translateY(-2px);
    box-shadow: 0 4px 12px rgba(102, 126, 234, 0.4);
  }

  .btn-primary:disabled {
    opacity: 0.6;
    cursor: not-allowed;
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

  .integrations-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(400px, 1fr));
    gap: 20px;
  }

  .integration-card {
    background: var(--bg-primary);
    border-radius: 12px;
    padding: 20px;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
    transition: transform 0.2s ease, box-shadow 0.2s ease;
  }

  .integration-card:hover {
    transform: translateY(-2px);
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
  }

  .integration-header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    margin-bottom: 12px;
  }

  .integration-header h3 {
    margin: 0 0 4px 0;
    font-size: 18px;
    font-weight: 600;
    color: var(--text-primary);
  }

  .integration-type {
    font-size: 12px;
    color: var(--text-secondary);
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }

  .integration-status {
    display: flex;
    align-items: center;
  }

  .status-badge {
    padding: 4px 12px;
    border-radius: 12px;
    font-size: 12px;
    font-weight: 600;
  }

  .status-active {
    background: #d1fae5;
    color: #065f46;
  }

  .status-inactive {
    background: #fee2e2;
    color: #991b1b;
  }

  .integration-description {
    margin: 12px 0;
    color: var(--text-secondary);
    font-size: 14px;
    line-height: 1.5;
  }

  .integration-details {
    margin: 16px 0;
    padding: 16px;
    background: var(--bg-tertiary);
    border-radius: 8px;
  }

  .detail-item {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 8px;
    font-size: 14px;
  }

  .detail-item:last-child {
    margin-bottom: 0;
  }

  .detail-label {
    font-weight: 600;
    color: var(--text-secondary);
  }

  .api-key-container {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .api-key {
    font-family: 'Courier New', monospace;
    font-size: 12px;
    background: var(--bg-primary);
    padding: 4px 8px;
    border-radius: 4px;
    border-color: var(--border-color);
  }

  .api-key-hidden {
    font-family: 'Courier New', monospace;
    font-size: 12px;
    color: var(--text-secondary);
  }

  .btn-icon-small {
    background: none;
    border: none;
    padding: 4px;
    cursor: pointer;
    color: var(--text-secondary);
    border-radius: 4px;
    transition: background 0.2s ease, color 0.2s ease;
  }

  .btn-icon-small:hover {
    background: #e5e7eb;
    color: var(--text-primary);
  }

  .btn-icon-small svg {
    width: 16px;
    height: 16px;
  }

  .integration-actions {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
    margin-top: 16px;
    padding-top: 16px;
    border-color: var(--border-color);
  }

  .btn-secondary {
    padding: 8px 16px;
    background: var(--bg-tertiary);
    color: var(--text-primary);
    border: none;
    border-radius: 8px;
    font-weight: 600;
    font-size: 14px;
    cursor: pointer;
    transition: background 0.2s ease;
  }

  .btn-secondary:hover:not(:disabled) {
    background: var(--accent-color);
  }

  .btn-secondary:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .btn-small {
    padding: 6px 12px;
    font-size: 12px;
  }

  .btn-icon-only {
    background: var(--bg-tertiary);
    border: 1px solid var(--border-color);
    padding: 6px;
    cursor: pointer;
    color: var(--text-primary);
    border-radius: 6px;
    transition: background 0.2s ease, color 0.2s ease;
  }

  .btn-icon-only:hover {
    background: var(--bg-secondary);
    border-color: var(--accent-color);
    color: var(--accent-color);
    transform: translateY(-1px);
    color: var(--text-primary);
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
  }

  .modal-content {
    background: var(--bg-primary);
    border-radius: 12px;
    width: 90%;
    max-width: 500px;
    max-height: 90vh;
    overflow-y: auto;
    box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.1);
  }

  .modal-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 20px 24px;
    border-color: var(--border-color);
  }

  .modal-header h3 {
    margin: 0;
    font-size: 20px;
    font-weight: 600;
  }

  .modal-body {
    padding: 24px;
  }

  .form-error {
    background: #fee2e2;
    color: #991b1b;
    padding: 12px;
    border-radius: 8px;
    margin-bottom: 16px;
  }

  .form-group {
    margin-bottom: 20px;
  }

  .form-group label {
    display: block;
    margin-bottom: 8px;
    font-weight: 600;
    color: var(--text-primary);
  }

  .form-group input,
  .form-group textarea,
  .form-group select {
    width: 100%;
    padding: 10px 12px;
    border-color: var(--border-color);
    border-radius: 8px;
    font-size: 14px;
    transition: border-color 0.2s ease;
  }

  .form-group input[type="checkbox"] {
    width: auto;
    margin-right: 8px;
  }

  .form-group input:focus,
  
  .form-group textarea:focus,
  
  .form-group select:focus {
  
    outline: none;
  
    border-color: var(--accent-color);
  
    box-shadow: 0 0 0 3px rgba(8, 145, 178, 0.1);
  
  }
  

  .modal-footer {
    display: flex;
    justify-content: flex-end;
    gap: 12px;
    padding: 20px 24px;
    border-color: var(--border-color);
  }
</style>
