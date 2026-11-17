<script>
  import { onMount } from 'svelte';
  import PageHeader from './PageHeader.svelte';
  import { listPatchPanels, createPatchPanel, updatePatchPanel, deletePatchPanel, getLocations, getRacks } from '../lib/api.js';

  const MEDIA = [
    { value: 'copper', label: 'Copper' },
    { value: 'fibre', label: 'Fibre' },
  ];

  let panels = [];
  let locations = [];
  let racks = [];
  let loading = true;
  let error = null;
  let filterLocation = '';

  let showModal = false;
  let editing = null;
  let form = blankForm();
  let formError = null;
  let submitting = false;

  $: visible = filterLocation ? panels.filter((p) => String(p.location_id) === filterLocation) : panels;
  $: availableRacks = form.location_id ? racks.filter((r) => r.location_id === Number(form.location_id)) : [];
  $: selectedRack = racks.find((r) => r.id === Number(form.rack_id));

  function blankForm() {
    return { name: '', location_id: '', rack_id: '', rack_unit: '', rack_units: 1, port_count: 24, medium: 'copper', description: '' };
  }

  function locationName(id) {
    return locations.find((l) => l.id === id)?.name || 'Unknown';
  }

  function placement(panel) {
    if (!panel.rack_id) return 'Not racked';
    const rack = racks.find((r) => r.id === panel.rack_id)?.name || 'Unknown rack';
    if (panel.rack_unit == null) return rack;
    const units = panel.rack_units || 1;
    return units > 1 ? `${rack} (U${panel.rack_unit} to U${panel.rack_unit + units - 1})` : `${rack} (U${panel.rack_unit})`;
  }

  async function load() {
    loading = true;
    error = null;
    try {
      const [list, locs, rks] = await Promise.all([listPatchPanels(), getLocations(), getRacks()]);
      panels = list || [];
      locations = locs || [];
      racks = rks || [];
    } catch (err) {
      error = err.message;
    } finally {
      loading = false;
    }
  }

  function openAdd() {
    editing = null;
    form = blankForm();
    if (filterLocation) form.location_id = filterLocation;
    else if (locations.length === 1) form.location_id = String(locations[0].id);
    formError = null;
    showModal = true;
  }

  function openEdit(panel) {
    editing = panel;
    form = {
      name: panel.name,
      location_id: String(panel.location_id),
      rack_id: panel.rack_id ? String(panel.rack_id) : '',
      rack_unit: panel.rack_unit ?? '',
      rack_units: panel.rack_units || 1,
      port_count: panel.port_count,
      medium: panel.medium,
      description: panel.description || '',
    };
    formError = null;
    showModal = true;
  }

  function closeModal() {
    showModal = false;
    editing = null;
  }

  function onLocationChange() {
    if (!availableRacks.some((r) => String(r.id) === String(form.rack_id))) {
      form.rack_id = '';
      form.rack_unit = '';
    }
  }

  function toInt(value) {
    if (value === '' || value === null || value === undefined) return null;
    const n = parseInt(value, 10);
    return Number.isNaN(n) ? null : n;
  }

  async function save() {
    formError = null;
    if (!form.name.trim()) {
      formError = 'Enter a name.';
      return;
    }
    if (!form.location_id) {
      formError = 'Choose a location.';
      return;
    }
    const portCount = toInt(form.port_count);
    if (!portCount || portCount < 1) {
      formError = 'Enter the number of ports.';
      return;
    }
    const rackId = toInt(form.rack_id);
    const data = {
      name: form.name.trim(),
      location_id: Number(form.location_id),
      rack_id: rackId,
      rack_unit: rackId ? toInt(form.rack_unit) : null,
      rack_units: toInt(form.rack_units) || 1,
      port_count: portCount,
      medium: form.medium,
      description: form.description.trim() || null,
    };
    submitting = true;
    try {
      if (editing) await updatePatchPanel(editing.id, data);
      else await createPatchPanel(data);
      closeModal();
      await load();
    } catch (err) {
      formError = err.message;
    } finally {
      submitting = false;
    }
  }

  async function remove(panel) {
    const cables = panel.cable_count || 0;
    const message = cables
      ? `Delete patch panel "${panel.name}" and its ${cables} cable${cables === 1 ? '' : 's'}? Paths through it will be broken.`
      : `Delete patch panel "${panel.name}"?`;
    if (!confirm(message)) return;
    try {
      await deletePatchPanel(panel.id, { force: cables > 0 });
      await load();
    } catch (err) {
      alert(`Could not delete the patch panel: ${err.message}`);
    }
  }

  onMount(load);
</script>

<PageHeader title="Patch Panels">
  <svelte:fragment slot="actions">
    <button class="btn-primary" on:click={openAdd} disabled={loading}>
      <svg xmlns="http://www.w3.org/2000/svg" class="btn-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
      </svg>
      Add Patch Panel
    </button>
  </svelte:fragment>
</PageHeader>

<div class="admin-page patch-panels-page">
  {#if loading}
    <div class="loading">Loading patch panels…</div>
  {:else if error}
    <div class="error">Error: {error}</div>
  {:else}
    {#if locations.length > 1}
      <div class="filters">
        <label for="pp-filter-location">Location</label>
        <select id="pp-filter-location" bind:value={filterLocation}>
          <option value="">All locations</option>
          {#each locations as loc (loc.id)}
            <option value={String(loc.id)}>{loc.name}</option>
          {/each}
        </select>
      </div>
    {/if}
    {#if visible.length === 0}
      <div class="empty-state">
        <p>No patch panels yet. Click "Add Patch Panel" to record one; its ports can then be cabled on the front and rear.</p>
      </div>
    {:else}
      <div class="admin-data-table">
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th>Medium</th>
              <th>Ports</th>
              <th>Cables</th>
              <th>Location</th>
              <th>Rack</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {#each visible as panel (panel.id)}
              <tr>
                <td>
                  <a href="/admin/patch-panels/{panel.id}" class="name-link"><strong>{panel.name}</strong></a>
                  {#if panel.description}<div class="sub">{panel.description}</div>{/if}
                </td>
                <td>{panel.medium === 'fibre' ? 'Fibre' : 'Copper'}</td>
                <td>{panel.port_count}</td>
                <td>{panel.cable_count}</td>
                <td>{locationName(panel.location_id)}</td>
                <td>{placement(panel)}</td>
                <td>
                  <div class="table-actions">
                    <a href="/admin/patch-panels/{panel.id}" class="btn-icon-only" title="View ports">
                      <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z" />
                      </svg>
                    </a>
                    <button class="btn-icon-only" on:click={() => openEdit(panel)} title="Edit">
                      <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
                      </svg>
                    </button>
                    <button class="btn-icon-only btn-danger" on:click={() => remove(panel)} title="Delete">
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
  {/if}
</div>

{#if showModal}
  <!-- svelte-ignore a11y-no-static-element-interactions -->
  <!-- svelte-ignore a11y-no-noninteractive-element-interactions -->
  <div class="modal-overlay" tabindex="-1" on:click={closeModal} on:keydown={(e) => e.key === 'Escape' && closeModal()}>
    <!-- svelte-ignore a11y-no-noninteractive-element-interactions -->
    <div class="modal-content" role="dialog" aria-labelledby="pp-modal-title" on:click|stopPropagation on:keydown|stopPropagation>
      <div class="modal-header">
        <h3 id="pp-modal-title">{editing ? 'Edit Patch Panel' : 'Add Patch Panel'}</h3>
        <button class="btn-icon-only" on:click={closeModal} aria-label="Close">
          <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
          </svg>
        </button>
      </div>
      <form class="modal-body" on:submit|preventDefault={save}>
        {#if formError}<div class="form-error">{formError}</div>{/if}
        <div class="form-group">
          <label for="pp-name">Name *</label>
          <input id="pp-name" type="text" bind:value={form.name} maxlength="255" required />
        </div>
        <div class="form-row two">
          <div class="form-group">
            <label for="pp-ports">Ports *</label>
            <input id="pp-ports" type="number" min="1" max="288" bind:value={form.port_count} required />
            {#if editing}<small class="field-help">Ports above the new count must have no cables.</small>{/if}
          </div>
          <div class="form-group">
            <label for="pp-medium">Medium</label>
            <select id="pp-medium" bind:value={form.medium}>
              {#each MEDIA as m (m.value)}<option value={m.value}>{m.label}</option>{/each}
            </select>
          </div>
        </div>
        <div class="form-group">
          <label for="pp-location">Location *</label>
          <select id="pp-location" bind:value={form.location_id} on:change={onLocationChange} required>
            <option value="">Select a location</option>
            {#each locations as loc (loc.id)}<option value={String(loc.id)}>{loc.name}</option>{/each}
          </select>
        </div>
        <div class="form-row three">
          <div class="form-group">
            <label for="pp-rack">Rack</label>
            <select id="pp-rack" bind:value={form.rack_id} disabled={!form.location_id || availableRacks.length === 0}>
              <option value="">No rack</option>
              {#each availableRacks as rack (rack.id)}<option value={String(rack.id)}>{rack.name} ({rack.units}U)</option>{/each}
            </select>
          </div>
          <div class="form-group">
            <label for="pp-unit">Bottom U</label>
            <input id="pp-unit" type="number" min="1" max={selectedRack?.units || undefined} bind:value={form.rack_unit} disabled={!form.rack_id} />
          </div>
          <div class="form-group">
            <label for="pp-units">Size (U)</label>
            <input id="pp-units" type="number" min="1" max="10" bind:value={form.rack_units} disabled={!form.rack_id} />
          </div>
        </div>
        <div class="form-group">
          <label for="pp-description">Description</label>
          <textarea id="pp-description" rows="2" bind:value={form.description}></textarea>
        </div>
        <div class="modal-footer">
          <button type="button" class="btn-secondary" on:click={closeModal} disabled={submitting}>Cancel</button>
          <button type="submit" class="btn-primary" disabled={submitting}>{submitting ? 'Saving…' : editing ? 'Save' : 'Add'}</button>
        </div>
      </form>
    </div>
  </div>
{/if}

<style>
  .filters {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 14px;
    font-size: 13px;
    color: var(--text-secondary);
  }

  .filters select {
    padding: 5px 8px;
    font-size: 13px;
    color: var(--text-primary);
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-sm);
  }

  .name-link {
    color: var(--text-primary);
    text-decoration: none;
  }

  .name-link:hover {
    color: var(--accent-color);
  }

  .sub {
    font-size: 12px;
    color: var(--text-tertiary);
    margin-top: 2px;
  }

  .form-row {
    display: grid;
    gap: 12px;
  }

  .form-row.two {
    grid-template-columns: 1fr 1fr;
  }

  .form-row.three {
    grid-template-columns: 2fr 1fr 1fr;
  }

  .field-help {
    font-size: 12px;
    color: var(--text-tertiary);
  }

  .modal-footer {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
    padding: 12px 0 0;
  }

  @media (max-width: 640px) {
    .form-row.two,
    .form-row.three {
      grid-template-columns: 1fr;
    }
  }
</style>
