<script>
  import PageHeader from './PageHeader.svelte';
  import { getRacks, createRack, updateRack, deleteRack, getLocations } from '../lib/api.js';
  import { navigate } from '../lib/router.js';
  import { onMount } from 'svelte';

  let racks = [];
  let locations = [];
  let loading = true;
  let error = null;
  let showModal = false;
  let editingRack = null;
  let selectedLocationId = null;
  let formData = { name: '', units: 42, description: '', row: null, row_position: null, units_start_from_bottom: true };
  let formError = null;
  let racksByLocationAndRow = {};

  onMount(async () => {
    await Promise.all([loadRacks(), loadLocations()]);
  });

  async function loadRacks() {
    try {
      loading = true;
      error = null;
      racks = await getRacks();
    } catch (err) {
      error = err.message;
      console.error('Failed to load racks:', err);
    } finally {
      loading = false;
    }
  }

  async function loadLocations() {
    try {
      locations = await getLocations();
    } catch (err) {
      console.error('Failed to load locations:', err);
    }
  }

  function openAddModal() {
    editingRack = null;
    selectedLocationId = null;
    formData = { name: '', units: 42, description: '', row: null, row_position: null, units_start_from_bottom: true };
    formError = null;
    showModal = true;
  }

  function openEditModal(rack) {
    editingRack = rack;
    selectedLocationId = rack.location_id;
    formData = { 
      name: rack.name, 
      units: rack.units, 
      description: rack.description || '',
      row: rack.row || null,
      row_position: rack.row_position || null,
      // Default to true if field is missing (existing racks)
      units_start_from_bottom: rack.units_start_from_bottom !== false
    };
    formError = null;
    showModal = true;
  }

  function closeModal() {
    showModal = false;
    editingRack = null;
    selectedLocationId = null;
    formData = { name: '', units: 42, description: '', row: null, row_position: null, units_start_from_bottom: true };
    formError = null;
  }

  async function handleSubmit() {
    if (!formData.name.trim()) {
      formError = 'Name is required';
      return;
    }

    if (!selectedLocationId) {
      formError = 'Location is required';
      return;
    }

    if (formData.units < 1 || formData.units > 100) {
      formError = 'Units must be between 1 and 100';
      return;
    }

    try {
      formError = null;
      if (editingRack) {
        await updateRack(
          editingRack.id,
          formData.name,
          formData.units,
          formData.description,
          formData.row,
          formData.row_position,
          formData.units_start_from_bottom
        );
      } else {
        await createRack(
          selectedLocationId,
          formData.name,
          formData.units,
          formData.description,
          formData.row,
          formData.row_position,
          formData.units_start_from_bottom
        );
      }
      closeModal();
      await loadRacks();
    } catch (err) {
      formError = err.message;
    }
  }

  async function handleDelete(rack) {
    if (!confirm(`Are you sure you want to delete rack "${rack.name}"?`)) {
      return;
    }

    try {
      await deleteRack(rack.id);
      await loadRacks();
    } catch (err) {
      alert('Failed to delete rack: ' + err.message);
    }
  }

  function getLocationName(locationId) {
    if (locationId == null || locationId === undefined) return '(no location set)';
    const location = locations.find(l => l.id === locationId);
    return location ? location.name : 'Unknown location';
  }

  function viewRow(locationId, row) {
    navigate(`/admin/racks/rows/${locationId}/${row}`);
  }

  // Group racks by location and row; recompute when either racks or locations change
  $: {
    const _locations = locations;
    racksByLocationAndRow = racks.reduce((acc, rack) => {
      const key = `${rack.location_id}-${rack.row || 'no-row'}`;
      if (!acc[key]) {
        acc[key] = {
          location_id: rack.location_id,
          location_name: getLocationName(rack.location_id),
          row: rack.row,
          racks: []
        };
      }
      acc[key].racks.push(rack);
      return acc;
    }, {});
  }
</script>

<PageHeader title="Racks" />

<div class="racks-container">
  <div class="racks-header">
    <h2>Racks</h2>
    <button class="btn-primary" on:click={openAddModal}>
      <svg xmlns="http://www.w3.org/2000/svg" class="btn-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
      </svg>
      Add Rack
    </button>
  </div>

  {#if loading}
    <div class="loading">Loading racks...</div>
  {:else if error}
    <div class="error">Error: {error}</div>
  {:else if racks.length === 0}
    <div class="empty-state">
      <p>No racks found. Click "Add Rack" to create one.</p>
    </div>
  {:else}
    <div class="racks-container-view">
      {#each Object.values(racksByLocationAndRow) as group}
        <div class="location-row-group">
          <div class="group-header">
            <h3>{group.location_name}</h3>
            {#if group.row !== null && group.row !== undefined}
              <span class="row-badge">Row {group.row}</span>
              <button class="btn-view-row" on:click={() => viewRow(group.location_id, group.row)}>
                View Row
              </button>
            {/if}
          </div>
          <div class="racks-grid">
            {#each group.racks as rack}
              <div class="rack-card">
                <div class="rack-header">
                  <div>
                    <h3>{rack.name}</h3>
                    {#if rack.row_position !== null && rack.row_position !== undefined}
                      <p class="rack-position">Position {rack.row_position}</p>
                    {/if}
                  </div>
                  <div class="rack-actions">
                    <button class="btn-icon-only" on:click={() => navigate(`/admin/racks/${rack.id}`)} title="View">
                      <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z" />
                      </svg>
                    </button>
                    <button class="btn-icon-only" on:click={() => openEditModal(rack)} title="Edit">
                      <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
                      </svg>
                    </button>
                    <button class="btn-icon-only btn-danger" on:click={() => handleDelete(rack)} title="Delete">
                      <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                      </svg>
                    </button>
                  </div>
                </div>
                <div class="rack-info">
                  <div class="rack-info-item">
                    <span class="rack-info-label">Units:</span>
                    <span class="rack-info-value">{rack.units}U</span>
                  </div>
                </div>
                {#if rack.description}
                  <p class="rack-description">{rack.description}</p>
                {/if}
              </div>
            {/each}
          </div>
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
        <h3>{editingRack ? 'Edit Rack' : 'Add Rack'}</h3>
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
          <label for="rack-location">Location *</label>
          <select
            id="rack-location"
            bind:value={selectedLocationId}
            disabled={editingRack !== null}
            required
          >
            <option value="">Select a location</option>
            {#each locations as location}
              <option value={location.id}>{location.name}</option>
            {/each}
          </select>
        </div>
        <div class="form-group">
          <label for="rack-name">Name *</label>
          <input
            id="rack-name"
            type="text"
            bind:value={formData.name}
            placeholder="e.g., Rack 01, A1"
            required
          />
        </div>
        <div class="form-group">
          <label for="rack-units">Number of Units (U) *</label>
          <input
            id="rack-units"
            type="number"
            bind:value={formData.units}
            min="1"
            max="100"
            required
          />
        </div>
        <div class="form-row">
          <div class="form-group">
            <label for="rack-row">Row (Optional)</label>
            <input
              id="rack-row"
              type="number"
              bind:value={formData.row}
              min="1"
              placeholder="Row number"
            />
            <small class="field-help">Row number within the location</small>
          </div>
          <div class="form-group">
            <label for="rack-row-position">Position in Row (Optional)</label>
            <input
              id="rack-row-position"
              type="number"
              bind:value={formData.row_position}
              min="1"
              placeholder="Position"
              disabled={formData.row === null || formData.row === ''}
            />
            <small class="field-help">Position within the row (1, 2, 3...)</small>
          </div>
        </div>
        <fieldset class="form-group rack-unit-numbering-fieldset">
          <legend>Rack unit numbering</legend>
          <div class="toggle-group">
            <label class:active={formData.units_start_from_bottom}>
              <input
                type="radio"
                name="unit-numbering"
                value="bottom"
                checked={formData.units_start_from_bottom}
                on:change={() => (formData.units_start_from_bottom = true)}
              />
              1 at bottom (default)
            </label>
            <label class:active={!formData.units_start_from_bottom}>
              <input
                type="radio"
                name="unit-numbering"
                value="top"
                checked={!formData.units_start_from_bottom}
                on:change={() => (formData.units_start_from_bottom = false)}
              />
              1 at top
            </label>
          </div>
          <small class="field-help">Controls how rack units are numbered and rendered in rack and row views.</small>
        </fieldset>
        <div class="form-group">
          <label for="rack-description">Description</label>
          <textarea
            id="rack-description"
            bind:value={formData.description}
            placeholder="Optional description"
            rows="3"
          ></textarea>
        </div>
      </div>
      <div class="modal-footer">
        <button class="btn-secondary" on:click={closeModal}>Cancel</button>
        <button class="btn-primary" on:click={handleSubmit}>
          {editingRack ? 'Update' : 'Create'}
        </button>
      </div>
    </div>
  </div>
{/if}

<style>
  .racks-container {
    padding: 32px;
  }

  .racks-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 24px;
  }

  .racks-header h2 {
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

  .racks-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
    gap: 20px;
  }

  .rack-card {
    background: var(--bg-primary);
    border-radius: 12px;
    padding: 20px;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
    transition: transform 0.2s ease, box-shadow 0.2s ease;
  }

  .rack-card:hover {
    transform: translateY(-2px);
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
  }

  .rack-header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    margin-bottom: 12px;
  }

  .rack-header h3 {
    margin: 0 0 4px 0;
    font-size: 18px;
    font-weight: 600;
    color: var(--text-primary);
  }

  .rack-unit-numbering-fieldset {
    border: none;
    margin: 0;
    padding: 0;
    min-width: 0;
  }

  .rack-unit-numbering-fieldset legend {
    font-weight: 600;
    color: var(--text-primary);
    margin-bottom: 8px;
    padding: 0;
  }

  .rack-actions {
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

  .rack-info {
    display: flex;
    gap: 16px;
    margin-bottom: 12px;
  }

  .rack-info-item {
    display: flex;
    gap: 8px;
  }

  .rack-info-label {
    font-weight: 600;
    color: var(--text-secondary);
  }

  .rack-info-value {
    color: var(--text-primary);
  }

  .rack-description {
    margin: 0;
    color: var(--text-secondary);
    font-size: 14px;
    line-height: 1.5;
  }

  .racks-container-view {
    display: flex;
    flex-direction: column;
    gap: 32px;
  }

  .location-row-group {
    background: var(--bg-secondary);
    border-radius: 12px;
    padding: 20px;
  }

  .group-header {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 16px;
  }

  .group-header h3 {
    margin: 0;
    font-size: 20px;
    font-weight: 700;
    color: var(--text-primary);
  }

  .row-badge {
    padding: 4px 12px;
    background: var(--accent-color);
    color: white;
    border-radius: 6px;
    font-size: 12px;
    font-weight: 600;
  }

  .btn-view-row {
    padding: 6px 12px;
    background: var(--bg-primary);
    color: var(--accent-color);
    border: 1px solid var(--accent-color);
    border-radius: 6px;
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.2s ease;
  }

  .btn-view-row:hover {
    background: var(--accent-color);
    color: white;
  }

  .rack-position {
    margin: 4px 0 0 0;
    font-size: 12px;
    color: var(--text-secondary);
  }

  .form-row {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
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
  .form-group textarea,
  .form-group select {
    width: 100%;
    padding: 10px 12px;
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    font-size: 14px;
    color: var(--text-primary);
    transition: all 0.2s ease;
  }

  .form-group select:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }
  
  .form-group input::placeholder,
  .form-group textarea::placeholder {
    color: var(--text-secondary);
    opacity: 0.7;
  }

  .form-group input:focus,
  .form-group textarea:focus,
  .form-group select:focus {
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
