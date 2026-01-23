<script>
  import PageHeader from './PageHeader.svelte';
  import CodeEditor from './CodeEditor.svelte';
  import { 
    getScripts, 
    createScript, 
    updateScript, 
    deleteScript,
    getScript
  } from '../lib/api.js';
  import { onMount } from 'svelte';

  let scripts = [];
  let loading = true;
  let error = null;
  let showModal = false;
  let editingScript = null;
  let formData = { 
    name: '', 
    content: '',
    description: '',
    enabled: true,
    user_executable: false
  };
  let formError = null;

  async function loadScripts() {
    try {
      loading = true;
      error = null;
      console.log('Loading scripts...');
      scripts = await getScripts();
      console.log('Scripts loaded:', scripts);
    } catch (err) {
      error = err.message || 'Failed to load scripts';
      console.error('Failed to load scripts:', err);
      scripts = []; // Ensure scripts is an array even on error
    } finally {
      loading = false;
    }
  }

  function openAddModal() {
    editingScript = null;
    formData = { 
      name: '', 
      content: '',
      description: '',
      enabled: true,
      user_executable: false
    };
    formError = null;
    showModal = true;
  }

  function openEditModal(script) {
    editingScript = script;
    formData = { 
      name: script.name,
      content: script.content,
      description: script.description || '',
      enabled: script.enabled,
      user_executable: script.user_executable
    };
    formError = null;
    showModal = true;
  }

  function closeModal() {
    showModal = false;
    editingScript = null;
    formData = { 
      name: '', 
      content: '',
      description: '',
      enabled: true,
      user_executable: false
    };
    formError = null;
  }

  async function handleSubmit() {
    if (!formData.name.trim()) {
      formError = 'Name is required';
      return;
    }

    if (!formData.content.trim()) {
      formError = 'Script content is required';
      return;
    }

    try {
      formError = null;
      if (editingScript) {
        await updateScript(editingScript.id, formData);
      } else {
        await createScript(formData);
      }
      closeModal();
      await loadScripts();
    } catch (err) {
      formError = err.message;
    }
  }

  async function handleDelete(script) {
    if (!confirm(`Are you sure you want to delete script "${script.name}"?`)) {
      return;
    }

    try {
      await deleteScript(script.id);
      await loadScripts();
    } catch (err) {
      alert('Failed to delete script: ' + err.message);
    }
  }

  function formatFileSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(2) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(2) + ' MB';
  }

  onMount(async () => {
    await loadScripts();
  });
</script>

<PageHeader title="Scripts" />

<div class="scripts-container">
  <div class="scripts-header">
    <h2>Scripts</h2>
    <button class="btn-primary" on:click={openAddModal}>
      <svg xmlns="http://www.w3.org/2000/svg" class="btn-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
      </svg>
      Add Script
    </button>
  </div>

  {#if loading}
    <div class="loading">Loading scripts...</div>
  {:else if error}
    <div class="error">Error: {error}</div>
  {:else if scripts.length === 0}
    <div class="empty-state">
      <p>No scripts found. Click "Add Script" to create one.</p>
    </div>
  {:else}
    <div class="scripts-grid">
      {#each scripts as script}
        <div class="script-card">
          <div class="script-header">
            <div>
              <h3>{script.name}</h3>
              <div class="script-badges">
                {#if script.enabled}
                  <span class="badge badge-success">Enabled</span>
                {:else}
                  <span class="badge badge-disabled">Disabled</span>
                {/if}
                {#if script.user_executable}
                  <span class="badge badge-info">User Executable</span>
                {/if}
              </div>
            </div>
            <div class="script-actions">
              <button class="btn-icon-only" on:click={() => openEditModal(script)} title="Edit">
                <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
                </svg>
              </button>
              <button class="btn-icon-only btn-danger" on:click={() => handleDelete(script)} title="Delete">
                <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                </svg>
              </button>
            </div>
          </div>
          
          {#if script.description}
            <p class="script-description">{script.description}</p>
          {/if}

          <div class="script-details">
            <div class="detail-item">
              <span class="detail-label">Size:</span>
              <span>{formatFileSize(script.size_bytes)}</span>
            </div>
            <div class="detail-item">
              <span class="detail-label">Lines:</span>
              <span>{script.content ? script.content.split('\n').length : 0}</span>
            </div>
          </div>

          <div class="script-preview">
            <pre><code>{script.content ? (script.content.substring(0, 200) + (script.content.length > 200 ? '...' : '')) : ''}</code></pre>
          </div>
        </div>
      {/each}
    </div>
  {/if}
</div>

{#if showModal}
  <div class="modal-overlay" on:click={closeModal}>
    <div class="modal-content modal-large" on:click|stopPropagation>
      <div class="modal-header">
        <h3>{editingScript ? 'Edit Script' : 'Add Script'}</h3>
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
          <label for="script-name">Name *</label>
          <input
            id="script-name"
            type="text"
            bind:value={formData.name}
            placeholder="e.g., install-services.sh"
            required
          />
        </div>
        <div class="form-group">
          <label for="script-description">Description</label>
          <textarea
            id="script-description"
            bind:value={formData.description}
            placeholder="What does this script do?"
            rows="2"
          ></textarea>
        </div>
        <div class="form-group">
          <label for="script-content">Script Content *</label>
          <CodeEditor bind:value={formData.content} language="bash" theme="dark" />
          <p class="form-help">Use <code>{'$'}{'{SERVER_IP}'}</code>, <code>{'$'}{'{SERVER_MAC}'}</code>, <code>{'$'}{'{SERVER_ID}'}</code> for variable substitution</p>
        </div>
        <div class="form-group form-group-inline">
          <label>
            <input type="checkbox" bind:checked={formData.enabled} />
            Enabled
          </label>
          <label>
            <input type="checkbox" bind:checked={formData.user_executable} />
            User Executable (available via billing API)
          </label>
        </div>
      </div>
      <div class="modal-footer">
        <button class="btn-secondary" on:click={closeModal}>Cancel</button>
        <button class="btn-primary" on:click={handleSubmit}>
          {editingScript ? 'Update' : 'Create'}
        </button>
      </div>
    </div>
  </div>
{/if}

<style>
  .scripts-container {
    padding: 32px;
  }

  .scripts-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 24px;
  }

  .scripts-header h2 {
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

  .scripts-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(400px, 1fr));
    gap: 20px;
  }

  .script-card {
    background: var(--bg-primary);
    border-radius: 12px;
    padding: 20px;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
    transition: transform 0.2s ease, box-shadow 0.2s ease;
  }

  .script-card:hover {
    transform: translateY(-2px);
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
  }

  .script-header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    margin-bottom: 12px;
  }

  .script-header h3 {
    margin: 0 0 8px 0;
    font-size: 18px;
    font-weight: 600;
    color: var(--text-primary);
  }

  .script-badges {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
  }

  .badge {
    padding: 4px 10px;
    border-radius: 12px;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }

  .badge-success {
    background: #d1fae5;
    color: #065f46;
  }

  .badge-disabled {
    background: #fee2e2;
    color: #991b1b;
  }

  .badge-info {
    background: #dbeafe;
    color: #1e40af;
  }

  .script-actions {
    display: flex;
    gap: 8px;
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

  .script-description {
    margin: 12px 0;
    color: var(--text-secondary);
    font-size: 14px;
    line-height: 1.5;
  }

  .script-details {
    display: flex;
    gap: 16px;
    margin: 12px 0;
    padding: 12px;
    background: var(--bg-tertiary);
    border-radius: 8px;
  }

  .detail-item {
    display: flex;
    gap: 8px;
    font-size: 14px;
  }

  .detail-label {
    font-weight: 600;
    color: var(--text-secondary);
  }

  .script-preview {
    margin-top: 12px;
    padding: 12px;
    background: #1e293b;
    border-radius: 8px;
    max-height: 150px;
    overflow-y: auto;
  }

  .script-preview pre {
    margin: 0;
    font-family: 'Courier New', monospace;
    font-size: 12px;
    color: #e2e8f0;
    white-space: pre-wrap;
    word-wrap: break-word;
  }

  .script-preview code {
    font-family: inherit;
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

  .modal-large {
    max-width: 900px;
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

  .form-group-inline {
    display: flex;
    gap: 24px;
  }

  .form-group label {
    display: block;
    margin-bottom: 8px;
    font-weight: 600;
    color: var(--text-primary);
  }

  .form-group-inline label {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 0;
  }

  .form-group input,
  .form-group textarea {
    width: 100%;
    padding: 10px 12px;
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    font-size: 14px;
    color: var(--text-primary);
    transition: all 0.2s ease;
  }
  
  .form-group input::placeholder,
  .form-group textarea::placeholder {
    color: var(--text-secondary);
    opacity: 0.7;
  }

  .form-group input[type="checkbox"] {
    width: auto;
    margin: 0;
  }

  .form-group input:focus,
  .form-group textarea:focus {
    outline: none;
    border-color: var(--accent-color);
    box-shadow: 0 0 0 3px rgba(8, 145, 178, 0.1);
  }

  .code-editor-wrapper {
    border-color: var(--border-color);
    border-radius: 8px;
    overflow: hidden;
    min-height: 400px;
  }

  .code-editor-wrapper :global(.cm-editor) {
    height: 400px;
  }

  .code-editor-wrapper :global(.cm-scroller) {
    overflow: auto;
    font-family: 'Courier New', monospace;
    font-size: 14px;
  }

  .form-help {
    margin-top: 8px;
    font-size: 12px;
    color: var(--text-secondary);
  }

  .modal-footer {
    display: flex;
    justify-content: flex-end;
    gap: 12px;
    padding: 20px 24px;
    border-color: var(--border-color);
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
</style>
