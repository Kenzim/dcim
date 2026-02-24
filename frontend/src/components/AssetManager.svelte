<script>
  import PageHeader from './PageHeader.svelte';
  import {
    getAssets,
    getAssetLabels,
    getAssetFileUrl,
    uploadAsset,
    deleteAsset,
  } from '../lib/api.js';
  import { onMount } from 'svelte';

  let assets = [];
  let labels = [];
  let loading = true;
  let error = null;
  let filterLabel = '';
  let uploadFile = null;
  let uploadLabel = 'generic';
  let uploadDescription = '';
  let uploading = false;
  let uploadError = null;

  async function loadLabels() {
    try {
      labels = await getAssetLabels();
    } catch (e) {
      console.error('Failed to load labels', e);
    }
  }

  async function loadAssets() {
    try {
      loading = true;
      error = null;
      assets = await getAssets(filterLabel || null);
    } catch (err) {
      error = err.message || 'Failed to load assets';
      assets = [];
    } finally {
      loading = false;
    }
  }

  function onFilterChange() {
    loadAssets();
  }

  function onFileChange(e) {
    const input = e.target;
    uploadFile = input.files && input.files[0] ? input.files[0] : null;
    uploadError = null;
  }

  async function handleUpload() {
    if (!uploadFile) {
      uploadError = 'Choose an image file';
      return;
    }
    try {
      uploading = true;
      uploadError = null;
      await uploadAsset(uploadFile, uploadLabel, uploadDescription || null);
      uploadFile = null;
      uploadLabel = 'generic';
      uploadDescription = '';
      document.querySelector('.asset-upload-input')?.form?.reset();
      await loadAssets();
    } catch (err) {
      uploadError = err.message;
    } finally {
      uploading = false;
    }
  }

  async function handleDelete(asset) {
    if (!confirm(`Delete "${asset.filename}"?`)) return;
    try {
      await deleteAsset(asset.id);
      await loadAssets();
    } catch (err) {
      alert('Failed to delete: ' + err.message);
    }
  }

  function labelDisplay(value) {
    const o = labels.find((l) => l.value === value);
    return o ? o.label : value;
  }

  onMount(async () => {
    await loadLabels();
    await loadAssets();
  });
</script>

<PageHeader title="Asset Manager" />

<div class="asset-manager">
  <div class="toolbar">
    <div class="filter-row">
      <label for="filter-label">Filter by use</label>
      <select id="filter-label" bind:value={filterLabel} on:change={onFilterChange}>
        <option value="">All</option>
        {#each labels as l}
          <option value={l.value}>{l.label}</option>
        {/each}
      </select>
    </div>

    <div class="upload-section">
      <h3>Upload image</h3>
      <form
        class="upload-form"
        on:submit|preventDefault={handleUpload}
      >
        <input
          type="file"
          class="asset-upload-input"
          accept=".jpg,.jpeg,.png,.gif,.webp,.svg"
          on:change={onFileChange}
        />
        <div class="upload-fields">
          <select bind:value={uploadLabel}>
            {#each labels as l}
              <option value={l.value}>{l.label}</option>
            {/each}
          </select>
          <input
            type="text"
            placeholder="Description (optional)"
            bind:value={uploadDescription}
          />
          <button type="submit" class="btn-primary" disabled={uploading || !uploadFile}>
            {uploading ? 'Uploading…' : 'Upload'}
          </button>
        </div>
        {#if uploadError}
          <p class="form-error">{uploadError}</p>
        {/if}
      </form>
    </div>
  </div>

  {#if loading}
    <div class="loading">Loading assets…</div>
  {:else if error}
    <div class="error">{error}</div>
  {:else if assets.length === 0}
    <div class="empty-state">
      <p>No images yet. Upload one above or clear the filter.</p>
    </div>
  {:else}
    <div class="asset-grid">
      {#each assets as asset}
        <div class="asset-card">
          <div class="asset-preview">
            <img
              src={getAssetFileUrl(asset.id)}
              alt={asset.filename}
              loading="lazy"
            />
          </div>
          <div class="asset-info">
            <span class="asset-filename" title={asset.filename}>{asset.filename}</span>
            <span class="asset-label">{labelDisplay(asset.label)}</span>
            {#if asset.description}
              <p class="asset-description" title={asset.description}>{asset.description}</p>
            {/if}
          </div>
          <div class="asset-actions">
            <button
              type="button"
              class="btn-icon-only btn-danger"
              on:click={() => handleDelete(asset)}
              title="Delete"
            >
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

<style>
  .asset-manager {
    padding: 32px;
  }

  .toolbar {
    margin-bottom: 24px;
    display: flex;
    flex-wrap: wrap;
    gap: 24px;
    align-items: flex-start;
  }

  .filter-row {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .filter-row label {
    color: var(--text-secondary);
    font-size: 14px;
  }

  .filter-row select {
    padding: 8px 12px;
    border-radius: 6px;
    border: 1px solid var(--border-color);
    background: var(--bg-primary);
    color: var(--text-primary);
    font-size: 14px;
  }

  .upload-section {
    flex: 1;
    min-width: 280px;
  }

  .upload-section h3 {
    margin: 0 0 12px 0;
    font-size: 16px;
    font-weight: 600;
    color: var(--text-primary);
  }

  .upload-form {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-start;
    gap: 12px;
  }

  .upload-form input[type="file"] {
    font-size: 14px;
    color: var(--text-secondary);
  }

  .upload-fields {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
  }

  .upload-fields select,
  .upload-fields input[type="text"] {
    padding: 8px 12px;
    border-radius: 6px;
    border: 1px solid var(--border-color);
    background: var(--bg-primary);
    color: var(--text-primary);
    font-size: 14px;
  }

  .upload-fields input[type="text"] {
    min-width: 160px;
  }

  .btn-primary {
    padding: 8px 16px;
    background: var(--accent-color);
    color: white;
    border: none;
    border-radius: 6px;
    font-weight: 600;
    cursor: pointer;
  }

  .btn-primary:hover:not(:disabled) {
    filter: brightness(1.05);
  }

  .btn-primary:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .form-error {
    color: var(--danger-color);
    font-size: 14px;
    margin: 4px 0 0 0;
    width: 100%;
  }

  .loading,
  .error,
  .empty-state {
    text-align: center;
    padding: 48px;
    color: var(--text-secondary);
  }

  .error {
    color: var(--danger-color);
  }

  .asset-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
    gap: 20px;
  }

  .asset-card {
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    transition: box-shadow 0.2s ease;
  }

  .asset-card:hover {
    box-shadow: var(--shadow-md);
  }

  .asset-preview {
    aspect-ratio: 1;
    background: var(--bg-tertiary);
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
  }

  .asset-preview img {
    width: 100%;
    height: 100%;
    object-fit: contain;
  }

  .asset-info {
    padding: 12px;
    flex: 1;
    min-width: 0;
  }

  .asset-filename {
    display: block;
    font-weight: 600;
    color: var(--text-primary);
    font-size: 14px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .asset-label {
    display: inline-block;
    margin-top: 4px;
    font-size: 12px;
    color: var(--text-secondary);
    background: var(--bg-tertiary);
    padding: 2px 8px;
    border-radius: 4px;
  }

  .asset-description {
    margin: 8px 0 0 0;
    font-size: 12px;
    color: var(--text-secondary);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .asset-actions {
    padding: 8px 12px;
    border-top: 1px solid var(--border-color);
    display: flex;
    justify-content: flex-end;
  }

  .btn-icon-only {
    padding: 6px;
    border: none;
    background: transparent;
    color: var(--text-secondary);
    border-radius: 6px;
    cursor: pointer;
  }

  .btn-icon-only:hover {
    background: var(--bg-tertiary);
    color: var(--text-primary);
  }

  .btn-icon-only.btn-danger:hover {
    background: var(--danger-bg);
    color: var(--danger-color);
  }

  .btn-icon-only svg {
    width: 18px;
    height: 18px;
  }
</style>
