<script>
  import PageHeader from './PageHeader.svelte';
  import { getLocations, createLocation, updateLocation, deleteLocation } from '../lib/api.js';
  import { navigate } from '../lib/router.js';
  import { onMount } from 'svelte';

  let locations = [];
  let loading = true;
  let error = null;
  let showModal = false;
  let editingLocation = null;
  let formData = { name: '', description: '' };
  let formError = null;

  onMount(async () => {
    await loadLocations();
  });

  async function loadLocations() {
    try {
      loading = true;
      error = null;
      locations = await getLocations();
    } catch (err) {
      error = err.message;
      console.error('Failed to load locations:', err);
    } finally {
      loading = false;
    }
  }

  function openAddModal() {
    editingLocation = null;
    formData = { name: '', description: '' };
    formError = null;
    showModal = true;
  }

  function openEditModal(location) {
    editingLocation = location;
    formData = { name: location.name, description: location.description || '' };
    formError = null;
    showModal = true;
  }

  function closeModal() {
    showModal = false;
    editingLocation = null;
    formData = { name: '', description: '' };
    formError = null;
  }

  async function handleSubmit() {
    if (!formData.name.trim()) {
      formError = 'Name is required';
      return;
    }

    try {
      formError = null;
      if (editingLocation) {
        await updateLocation(editingLocation.id, formData.name, formData.description);
      } else {
        await createLocation(formData.name, formData.description);
      }
      closeModal();
      await loadLocations();
    } catch (err) {
      formError = err.message;
    }
  }

  async function handleDelete(location) {
    if (!confirm(`Are you sure you want to delete location "${location.name}"?`)) {
      return;
    }

    try {
      await deleteLocation(location.id);
      await loadLocations();
    } catch (err) {
      alert('Failed to delete location: ' + err.message);
    }
  }
</script>

<PageHeader title="Locations" />

<div class="locations-container">
  <div class="locations-header">
    <h2>Locations</h2>
    <button class="btn-primary" on:click={openAddModal}>
      <svg xmlns="http://www.w3.org/2000/svg" class="btn-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
      </svg>
      Add Location
    </button>
  </div>

  {#if loading}
    <div class="loading">Loading locations...</div>
  {:else if error}
    <div class="error">Error: {error}</div>
  {:else if locations.length === 0}
    <div class="empty-state">
      <p>No locations found. Click "Add Location" to create one.</p>
    </div>
  {:else}
    <div class="locations-grid">
      {#each locations as location}
        <div
          class="location-card"
          role="button"
          tabindex="0"
          on:click={() => navigate(`/admin/locations/${location.id}`)}
          on:keydown={(e) => {
            if (e.key === 'Enter' || e.key === ' ') {
              e.preventDefault();
              navigate(`/admin/locations/${location.id}`);
            }
          }}
        >
          <div class="location-header">
            <h3>{location.name}</h3>
            <!-- svelte-ignore a11y-no-noninteractive-element-interactions -->
            <div class="location-actions" role="group" on:click|stopPropagation on:keydown|stopPropagation>
              <button class="btn-icon-only" on:click={() => openEditModal(location)} title="Edit">
                <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
                </svg>
              </button>
              <button class="btn-icon-only btn-danger" on:click={() => handleDelete(location)} title="Delete">
                <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                </svg>
              </button>
            </div>
          </div>
          {#if location.description}
            <p class="location-description">{location.description}</p>
          {/if}
        </div>
      {/each}
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
    <div class="modal-content" on:click|stopPropagation on:keydown|stopPropagation>
      <div class="modal-header">
        <h3>{editingLocation ? 'Edit Location' : 'Add Location'}</h3>
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
          <label for="location-name">Name *</label>
          <input
            id="location-name"
            type="text"
            bind:value={formData.name}
            placeholder="e.g., Data Center A, Rack 1"
            required
          />
        </div>
        <div class="form-group">
          <label for="location-description">Description</label>
          <textarea
            id="location-description"
            bind:value={formData.description}
            placeholder="Optional description"
            rows="3"
          ></textarea>
        </div>
      </div>
      <div class="modal-footer">
        <button class="btn-secondary" on:click={closeModal}>Cancel</button>
        <button class="btn-primary" on:click={handleSubmit}>
          {editingLocation ? 'Update' : 'Create'}
        </button>
      </div>
    </div>
  </div>
{/if}

<style>
  .locations-container {
    padding: 32px;
  }

  .locations-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 24px;
  }

  .locations-header h2 {
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
    border: 1px solid var(--accent-color);
    border-radius: 8px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.2s ease;
  }

  .btn-primary:hover:not(:disabled) {
    background: var(--accent-dark);
    border-color: var(--accent-dark);
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

  .locations-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
    gap: 20px;
  }

  .location-card {
    background: var(--bg-primary);
    border-radius: 12px;
    padding: 20px;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
    transition: transform 0.2s ease, box-shadow 0.2s ease;
  }

  .location-card:hover {
    transform: translateY(-2px);
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
  }

  .location-card[role="button"] {
    cursor: pointer;
  }

  .location-header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    margin-bottom: 12px;
  }

  .location-header h3 {
    margin: 0;
    font-size: 18px;
    font-weight: 600;
    color: var(--text-primary);
  }

  .location-actions {
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

  .location-description {
    margin: 0;
    color: var(--text-secondary);
    font-size: 14px;
    line-height: 1.5;
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
  }

  .modal-content {
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: 12px;
    width: 90%;
    max-width: 500px;
    max-height: 90vh;
    overflow-y: auto;
    box-shadow: var(--shadow-xl);
  }

  .modal-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 20px 24px;
    border-bottom: 1px solid var(--border-color);
    background: var(--bg-primary);
  }

  .modal-header h3 {
    margin: 0;
    font-size: 20px;
    font-weight: 700;
    color: var(--text-primary);
  }
  
  .modal-header .btn-icon-only {
    background: var(--bg-tertiary);
    border: 1px solid var(--border-color);
    padding: 8px;
    color: var(--text-primary);
  }
  
  .modal-header .btn-icon-only:hover {
    background: var(--danger-color);
    border-color: var(--danger-color);
    color: white;
  }

  .modal-body {
    padding: 24px;
  }

  .form-error {
    background: var(--danger-bg);
    color: var(--danger-text);
    padding: 12px;
    border-radius: 8px;
    margin-bottom: 16px;
    border: 1px solid var(--danger-color);
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

  .form-group input:focus,
  .form-group textarea:focus {
    outline: none;
    border-color: var(--accent-color);
    box-shadow: var(--focus-ring-accent);
  }

  .modal-footer {
    display: flex;
    justify-content: flex-end;
    gap: 12px;
    padding: 20px 24px;
    border-top: 1px solid var(--border-color);
    background: var(--bg-primary);
  }

  .btn-secondary {
    padding: 10px 20px;
    background: var(--bg-primary);
    color: var(--text-primary);
    border: 2px solid var(--accent-color);
    border-radius: 8px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.2s ease;
    box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
  }

  .btn-secondary:hover:not(:disabled) {
    background: var(--accent-color);
    border-color: var(--accent-color);
    color: white;
    transform: translateY(-1px);
    box-shadow: var(--shadow-md);
  }
</style>



