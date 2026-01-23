<script>
  import PageHeader from './PageHeader.svelte';
  import { getOSTemplates, reloadOSTemplates } from '../lib/api.js';
  import { onMount } from 'svelte';

  let templates = [];
  let loading = true;
  let error = null;
  let reloading = false;
  let expandedTemplate = null;

  onMount(async () => {
    await loadTemplates();
  });

  async function loadTemplates() {
    try {
      loading = true;
      error = null;
      templates = await getOSTemplates();
    } catch (err) {
      error = err.message;
      console.error('Failed to load templates:', err);
    } finally {
      loading = false;
    }
  }

  async function handleReload() {
    reloading = true;
    try {
      await reloadOSTemplates();
      await loadTemplates();
      alert('Templates reloaded successfully');
    } catch (err) {
      alert('Failed to reload templates: ' + err.message);
    } finally {
      reloading = false;
    }
  }

  function toggleTemplate(templateId) {
    expandedTemplate = expandedTemplate === templateId ? null : templateId;
  }

  function getParameterTypeLabel(type) {
    const labels = {
      text: 'Text',
      password: 'Password',
      select: 'Select',
      number: 'Number',
      boolean: 'Boolean'
    };
    return labels[type] || type;
  }
</script>

<PageHeader title="OS Installation Templates" />

{#if loading}
  <div class="content-body">
    <div class="loading">Loading templates...</div>
  </div>
{:else if error}
  <div class="content-body">
    <div class="error">Error: {error}</div>
    <button class="btn-primary" on:click={loadTemplates}>Retry</button>
  </div>
{:else}
  <div class="content-body">
    <div class="header-actions">
      <button class="btn-secondary" on:click={handleReload} disabled={reloading}>
        {reloading ? 'Reloading...' : 'Reload Templates'}
      </button>
    </div>

    {#if templates.length === 0}
      <div class="no-templates">
        <p>No OS installation templates found.</p>
        <p class="help-text">Create templates in the <code>os_templates/</code> directory on the server.</p>
      </div>
    {:else}
      <div class="templates-grid">
        {#each templates as template}
          <div class="template-card">
            <div class="template-header">
              <div class="template-title">
                <h3>{template.name}</h3>
                <span class="template-id">{template.id}</span>
              </div>
              <span class="os-type-badge" class:windows={template.os_type === 'windows'} class:linux={template.os_type === 'linux'}>
                {template.os_type}
              </span>
            </div>
            
            <p class="template-description">{template.description}</p>
            
            {#if Object.keys(template.parameters).length > 0}
              <div class="template-parameters">
                <button class="btn-text" on:click={() => toggleTemplate(template.id)}>
                  {expandedTemplate === template.id ? 'Hide' : 'Show'} Parameters ({Object.keys(template.parameters).length})
                </button>
                
                {#if expandedTemplate === template.id}
                  <div class="parameters-list">
                    {#each Object.entries(template.parameters) as [paramName, param]}
                      <div class="parameter-item">
                        <div class="parameter-header">
                          <strong>{param.label}</strong>
                          <span class="parameter-type">{getParameterTypeLabel(param.type)}</span>
                          {#if param.required}
                            <span class="required-badge">Required</span>
                          {/if}
                        </div>
                        {#if param.help}
                          <p class="parameter-help">{param.help}</p>
                        {/if}
                        {#if param.type === 'select' && param.options}
                          <div class="parameter-options">
                            Options: {param.options.join(', ')}
                          </div>
                        {/if}
                        {#if param.default !== null && param.default !== undefined}
                          <div class="parameter-default">
                            Default: <code>{String(param.default)}</code>
                          </div>
                        {/if}
                      </div>
                    {/each}
                  </div>
                {/if}
              </div>
            {:else}
              <p class="no-parameters">No parameters required</p>
            {/if}
            
            {#if template.kernel_url || template.initrd_url}
              <div class="template-boot-info">
                <strong>Boot Configuration:</strong>
                {#if template.kernel_url}
                  <div class="boot-url">Kernel: <code>{template.kernel_url}</code></div>
                {/if}
                {#if template.initrd_url}
                  <div class="boot-url">Initrd: <code>{template.initrd_url}</code></div>
                {/if}
              </div>
            {/if}
          </div>
        {/each}
      </div>
    {/if}
  </div>
{/if}

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

  .header-actions {
    display: flex;
    justify-content: flex-end;
    margin-bottom: 24px;
  }

  .no-templates {
    padding: 48px;
    text-align: center;
    background: var(--bg-primary);
    border-radius: 12px;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
  }

  .no-templates p {
    margin: 8px 0;
    color: var(--text-secondary);
  }

  .help-text {
    font-size: 14px;
  }

  code {
    background: #f3f4f6;
    padding: 2px 6px;
    border-radius: 4px;
    font-family: 'Courier New', monospace;
    font-size: 13px;
  }

  .templates-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(400px, 1fr));
    gap: 24px;
  }

  .template-card {
    background: var(--bg-primary);
    border-radius: 12px;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
    padding: 24px;
    transition: box-shadow 0.2s ease;
  }

  .template-card:hover {
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
  }

  .template-header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    margin-bottom: 12px;
  }

  .template-title h3 {
    margin: 0 0 4px 0;
    font-size: 20px;
    font-weight: 700;
    color: var(--text-primary);
  }

  .template-id {
    font-size: 12px;
    color: var(--text-secondary);
    font-family: 'Courier New', monospace;
  }

  .os-type-badge {
    padding: 4px 12px;
    border-radius: 12px;
    font-size: 12px;
    font-weight: 600;
    text-transform: uppercase;
  }

  .os-type-badge.windows {
    background: #0078d4;
    color: white;
  }

  .os-type-badge.linux {
    background: #fcc624;
    color: #000;
  }

  .template-description {
    color: var(--text-secondary);
    margin: 12px 0;
    line-height: 1.5;
  }

  .template-parameters {
    margin-top: 16px;
    padding-top: 16px;
    border-color: var(--border-color);
  }

  .no-parameters {
    color: #9ca3af;
    font-size: 14px;
    font-style: italic;
    margin: 12px 0 0 0;
  }

  .parameters-list {
    margin-top: 12px;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .parameter-item {
    padding: 12px;
    background: var(--bg-tertiary);
    border-radius: 8px;
    border-left: 3px solid var(--accent-color);
  }

  .parameter-header {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 4px;
  }

  .parameter-header strong {
    color: var(--text-primary);
  }

  .parameter-type {
    font-size: 11px;
    padding: 2px 6px;
    background: #e5e7eb;
    border-radius: 4px;
    color: var(--text-primary);
    font-weight: 600;
  }

  .required-badge {
    font-size: 11px;
    padding: 2px 6px;
    background: #fee2e2;
    color: #991b1b;
    border-radius: 4px;
    font-weight: 600;
  }

  .parameter-help {
    font-size: 13px;
    color: var(--text-secondary);
    margin: 4px 0;
  }

  .parameter-options {
    font-size: 12px;
    color: var(--text-primary);
    margin-top: 4px;
  }

  .parameter-default {
    font-size: 12px;
    color: #059669;
    margin-top: 4px;
  }

  .template-boot-info {
    margin-top: 16px;
    padding-top: 16px;
    border-color: var(--border-color);
    font-size: 13px;
  }

  .template-boot-info strong {
    color: var(--text-primary);
    display: block;
    margin-bottom: 8px;
  }

  .boot-url {
    margin: 4px 0;
    color: var(--text-secondary);
  }

  .boot-url code {
    font-size: 11px;
    word-break: break-all;
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

  .btn-primary:hover:not(:disabled) {
    background: #2563eb;
  }

  .btn-primary:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .btn-secondary {
    padding: 10px 20px;
    background: #f3f4f6;
    color: var(--text-primary);
    border: none;
    border-radius: 8px;
    cursor: pointer;
    font-weight: 600;
    transition: background 0.2s ease;
  }

  .btn-secondary:hover:not(:disabled) {
    background: #e5e7eb;
  }

  .btn-secondary:disabled {
    opacity: 0.5;
    cursor: not-allowed;
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
    font-size: 14px;
  }

  .btn-text:hover {
    background: #eff6ff;
  }
</style>
