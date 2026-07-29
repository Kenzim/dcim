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
  let dragOver = false;
  let fileInput;

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

  function setUploadFile(file) {
    if (!file) {
      uploadFile = null;
      return;
    }
    const ok = /\.(jpe?g|png|gif|webp|svg)$/i.test(file.name);
    if (!ok) {
      uploadError = 'Choose a JPG, PNG, GIF, WebP, or SVG image';
      uploadFile = null;
      return;
    }
    uploadFile = file;
    uploadError = null;
  }

  function onFileChange(e) {
    const input = e.target;
    setUploadFile(input.files && input.files[0] ? input.files[0] : null);
  }

  function onDrop(e) {
    e.preventDefault();
    dragOver = false;
    const file = e.dataTransfer?.files?.[0];
    if (file) {
      setUploadFile(file);
      if (fileInput) {
        const dt = new DataTransfer();
        dt.items.add(file);
        fileInput.files = dt.files;
      }
    }
  }

  function clearUploadFile() {
    uploadFile = null;
    uploadError = null;
    if (fileInput) fileInput.value = '';
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
      if (fileInput) fileInput.value = '';
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

  function formatDate(iso) {
    if (!iso) return '';
    try {
      return new Date(iso).toLocaleDateString(undefined, {
        year: 'numeric',
        month: 'short',
        day: 'numeric',
      });
    } catch {
      return '';
    }
  }

  onMount(async () => {
    await loadLabels();
    await loadAssets();
  });
</script>

<div class="asset-manager-page">
  <PageHeader title="Asset Manager" />

  <div class="page-content">
    <div class="controls">
      <section class="panel filter-panel">
        <div class="panel-head">
          <h2>Library</h2>
          {#if !loading}
            <span class="count-chip">{assets.length} {assets.length === 1 ? 'image' : 'images'}</span>
          {/if}
        </div>
        <div class="form-group filter-group">
          <label for="filter-label">Filter by use</label>
          <select id="filter-label" bind:value={filterLabel} on:change={onFilterChange}>
            <option value="">All uses</option>
            {#each labels as l}
              <option value={l.value}>{l.label}</option>
            {/each}
          </select>
        </div>
      </section>

      <section class="panel upload-panel">
        <div class="panel-head">
          <h2>Upload image</h2>
          <p>JPG, PNG, GIF, WebP, or SVG</p>
        </div>

        <form class="upload-form" on:submit|preventDefault={handleUpload}>
          <div
            class="dropzone"
            class:drag-over={dragOver}
            class:has-file={!!uploadFile}
            role="button"
            tabindex="0"
            on:dragover|preventDefault={() => (dragOver = true)}
            on:dragleave|preventDefault={() => (dragOver = false)}
            on:drop={onDrop}
            on:click={() => fileInput?.click()}
            on:keydown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                fileInput?.click();
              }
            }}
          >
            <input
              id="asset-file"
              type="file"
              class="asset-upload-input"
              accept=".jpg,.jpeg,.png,.gif,.webp,.svg"
              bind:this={fileInput}
              on:change={onFileChange}
              on:click|stopPropagation
            />
            {#if uploadFile}
              <div class="dropzone-selected">
                <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z" />
                </svg>
                <div class="dropzone-file-meta">
                  <span class="dropzone-filename" title={uploadFile.name}>{uploadFile.name}</span>
                  <span class="dropzone-hint">Click to replace, or drop another file</span>
                </div>
                <button
                  type="button"
                  class="btn-clear-file"
                  on:click|stopPropagation={clearUploadFile}
                  title="Clear file"
                >
                  <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
                  </svg>
                </button>
              </div>
            {:else}
              <svg class="dropzone-icon" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12" />
              </svg>
              <span class="dropzone-title">Drop an image here</span>
              <span class="dropzone-hint">or click to browse</span>
            {/if}
          </div>

          <div class="upload-fields">
            <div class="form-group">
              <label for="upload-label">Use</label>
              <select id="upload-label" bind:value={uploadLabel}>
                {#each labels as l}
                  <option value={l.value}>{l.label}</option>
                {/each}
              </select>
            </div>
            <div class="form-group grow">
              <label for="upload-description">Description</label>
              <input
                id="upload-description"
                type="text"
                placeholder="Optional description"
                bind:value={uploadDescription}
              />
            </div>
            <div class="upload-submit">
              <button type="submit" class="btn-primary" disabled={uploading || !uploadFile}>
                {#if uploading}
                  <span class="spinner spinner-small"></span>
                  Uploading…
                {:else}
                  <svg xmlns="http://www.w3.org/2000/svg" class="btn-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12" />
                  </svg>
                  Upload
                {/if}
              </button>
            </div>
          </div>

          {#if uploadError}
            <div class="alert alert-error upload-alert">
              <svg xmlns="http://www.w3.org/2000/svg" class="alert-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
              </svg>
              {uploadError}
            </div>
          {/if}
        </form>
      </section>
    </div>

    {#if loading}
      <div class="loading-container">
        <div class="spinner"></div>
        <p>Loading assets…</p>
      </div>
    {:else if error}
      <div class="alert alert-error">
        <svg xmlns="http://www.w3.org/2000/svg" class="alert-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
        </svg>
        {error}
      </div>
    {:else if assets.length === 0}
      <div class="empty-state">
        <svg xmlns="http://www.w3.org/2000/svg" class="empty-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z" />
        </svg>
        <h3>No images yet</h3>
        <p>{filterLabel ? 'No assets match this filter. Try another use, or upload one above.' : 'Upload an image above to start building your library.'}</p>
      </div>
    {:else}
      <div class="asset-grid">
        {#each assets as asset}
          <article class="asset-card">
            <div class="asset-preview">
              <img
                src={getAssetFileUrl(asset.id)}
                alt={asset.filename}
                loading="lazy"
              />
              <div class="asset-preview-actions">
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
            <div class="asset-info">
              <span class="asset-filename" title={asset.filename}>{asset.filename}</span>
              <div class="asset-meta">
                <span class="asset-label">{labelDisplay(asset.label)}</span>
                {#if asset.created_at}
                  <span class="asset-date">{formatDate(asset.created_at)}</span>
                {/if}
              </div>
              {#if asset.description}
                <p class="asset-description" title={asset.description}>{asset.description}</p>
              {/if}
            </div>
          </article>
        {/each}
      </div>
    {/if}
  </div>
</div>

<style>
  .asset-manager-page {
    min-height: 100vh;
    background: var(--bg-secondary);
  }

  .page-content {
    padding: 32px;
  }

  @media (max-width: 768px) {
    .page-content {
      padding: 16px;
    }
  }

  .controls {
    display: grid;
    grid-template-columns: minmax(220px, 280px) 1fr;
    gap: 20px;
    margin-bottom: 28px;
    align-items: stretch;
  }

  @media (max-width: 900px) {
    .controls {
      grid-template-columns: 1fr;
    }
  }

  .panel {
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: 12px;
    padding: 20px;
    box-shadow: var(--shadow-sm);
  }

  .panel-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 16px;
  }

  .panel-head h2 {
    margin: 0;
    font-size: 16px;
    font-weight: 700;
    color: var(--text-primary);
    letter-spacing: -0.2px;
  }

  .panel-head p {
    margin: 0;
    font-size: 12px;
    color: var(--text-tertiary);
  }

  .count-chip {
    display: inline-flex;
    align-items: center;
    padding: 4px 10px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 600;
    background: var(--info-bg);
    color: var(--info-text);
  }

  .filter-group {
    margin-bottom: 0;
  }

  .upload-form {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .dropzone {
    position: relative;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 6px;
    min-height: 120px;
    padding: 20px;
    border: 2px dashed var(--border-color);
    border-radius: 10px;
    background: var(--bg-secondary);
    cursor: pointer;
    transition: border-color 0.2s ease, background 0.2s ease, box-shadow 0.2s ease;
  }

  .dropzone:hover,
  .dropzone.drag-over {
    border-color: var(--accent-color);
    background: var(--info-bg);
    box-shadow: var(--focus-ring-accent);
  }

  .dropzone.has-file {
    border-style: solid;
    border-color: var(--border-color);
    align-items: stretch;
  }

  .asset-upload-input {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border: 0;
  }

  .dropzone-icon {
    width: 36px;
    height: 36px;
    color: var(--accent-light);
    margin-bottom: 4px;
  }

  .dropzone-title {
    font-size: 14px;
    font-weight: 600;
    color: var(--text-primary);
  }

  .dropzone-hint {
    font-size: 12px;
    color: var(--text-tertiary);
  }

  .dropzone-selected {
    display: flex;
    align-items: center;
    gap: 12px;
    width: 100%;
  }

  .dropzone-selected > svg {
    width: 28px;
    height: 28px;
    color: var(--accent-color);
    flex-shrink: 0;
  }

  .dropzone-file-meta {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
    flex: 1;
  }

  .dropzone-filename {
    font-size: 14px;
    font-weight: 600;
    color: var(--text-primary);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .btn-clear-file {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 32px;
    height: 32px;
    padding: 0;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background: var(--bg-primary);
    color: var(--text-secondary);
    cursor: pointer;
    flex-shrink: 0;
    transition: background 0.2s ease, color 0.2s ease, border-color 0.2s ease;
  }

  .btn-clear-file:hover {
    background: var(--danger-bg);
    border-color: var(--danger-color);
    color: var(--danger-color);
  }

  .btn-clear-file svg {
    width: 16px;
    height: 16px;
  }

  .upload-fields {
    display: grid;
    grid-template-columns: minmax(140px, 200px) 1fr auto;
    gap: 12px;
    align-items: end;
  }

  @media (max-width: 700px) {
    .upload-fields {
      grid-template-columns: 1fr;
    }

    .upload-submit {
      width: 100%;
    }

    .upload-submit .btn-primary {
      width: 100%;
    }
  }

  .upload-fields .form-group {
    margin-bottom: 0;
  }

  .upload-fields .form-group.grow {
    min-width: 0;
  }

  .upload-submit {
    display: flex;
    align-items: flex-end;
    padding-bottom: 0;
  }

  .btn-icon {
    width: 18px;
    height: 18px;
  }

  .upload-alert {
    margin-bottom: 0;
  }

  .loading-container {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 80px 20px;
    color: var(--text-secondary);
    gap: 12px;
  }

  .loading-container p {
    margin: 0;
    font-size: 14px;
  }

  .empty-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 72px 24px;
    text-align: center;
    color: var(--text-secondary);
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: 12px;
    box-shadow: var(--shadow-sm);
  }

  .empty-icon {
    width: 56px;
    height: 56px;
    margin-bottom: 12px;
    opacity: 0.45;
    color: var(--text-tertiary);
  }

  .empty-state h3 {
    font-size: 18px;
    font-weight: 600;
    margin: 0 0 8px;
    color: var(--text-primary);
  }

  .empty-state p {
    margin: 0;
    font-size: 14px;
    max-width: 360px;
    line-height: 1.5;
  }

  .asset-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    gap: 20px;
  }

  .asset-card {
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: 12px;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    box-shadow: var(--shadow-sm);
    transition: transform 0.2s ease, box-shadow 0.2s ease, border-color 0.2s ease;
  }

  .asset-card:hover {
    transform: translateY(-3px);
    box-shadow: var(--shadow-lg);
    border-color: var(--accent-color);
  }

  .asset-preview {
    position: relative;
    aspect-ratio: 1;
    background:
      linear-gradient(45deg, var(--bg-tertiary) 25%, transparent 25%),
      linear-gradient(-45deg, var(--bg-tertiary) 25%, transparent 25%),
      linear-gradient(45deg, transparent 75%, var(--bg-tertiary) 75%),
      linear-gradient(-45deg, transparent 75%, var(--bg-tertiary) 75%);
    background-size: 16px 16px;
    background-position: 0 0, 0 8px, 8px -8px, -8px 0;
    background-color: var(--bg-secondary);
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
  }

  .asset-preview img {
    width: 100%;
    height: 100%;
    object-fit: contain;
    transition: transform 0.25s ease;
  }

  .asset-card:hover .asset-preview img {
    transform: scale(1.03);
  }

  .asset-preview-actions {
    position: absolute;
    top: 10px;
    right: 10px;
    opacity: 1;
    transition: opacity 0.2s ease, transform 0.2s ease;
  }

  @media (hover: hover) and (pointer: fine) {
    .asset-preview-actions {
      opacity: 0;
      transform: translateY(-4px);
    }

    .asset-card:hover .asset-preview-actions,
    .asset-preview-actions:focus-within {
      opacity: 1;
      transform: translateY(0);
    }
  }

  .asset-info {
    padding: 14px 16px 16px;
    display: flex;
    flex-direction: column;
    gap: 8px;
    min-width: 0;
    flex: 1;
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

  .asset-meta {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    flex-wrap: wrap;
  }

  .asset-label {
    display: inline-flex;
    align-items: center;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.02em;
    text-transform: uppercase;
    color: var(--info-text);
    background: var(--info-bg);
    padding: 4px 10px;
    border-radius: 999px;
  }

  .asset-date {
    font-size: 12px;
    color: var(--text-tertiary);
  }

  .asset-description {
    margin: 0;
    font-size: 13px;
    line-height: 1.4;
    color: var(--text-secondary);
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
</style>
