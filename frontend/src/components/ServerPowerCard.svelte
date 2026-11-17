<script>
  import { onMount } from 'svelte';
  import {
    getServerPowerInputs,
    createServerPowerInput,
    updateServerPowerInput,
    deleteServerPowerInput,
    actionServerPowerInputOutlet,
    getPdus,
    getPdu
  } from '../lib/api.js';
  import { formatWatts, formatAmps } from '../lib/power.js';

  export let serverId;
  export let locationId = null;
  export let serverName = '';

  let loading = true;
  let error = '';
  let data = null;

  let pdus = [];
  let selectedPduId = '';
  let pduOutlets = [];
  let pduLoading = false;

  let showInputModal = false;
  let editingInput = null;
  let inputForm = { name: '', description: '', pdu_id: '', pdu_outlet_id: '' };
  let inputError = '';
  let saving = false;

  let pendingAction = null;
  let actionError = '';
  let actionBusy = false;

  async function load() {
    try {
      data = await getServerPowerInputs(serverId);
      error = '';
    } catch (err) {
      error = err.message || 'Could not load power inputs.';
    } finally {
      loading = false;
    }
  }

  async function loadPdus() {
    try {
      pdus = await getPdus(locationId);
    } catch (err) {
      pdus = [];
    }
  }

  async function loadPduOutlets(pduId) {
    if (!pduId) {
      pduOutlets = [];
      return;
    }
    pduLoading = true;
    try {
      const pdu = await getPdu(pduId);
      pduOutlets = pdu.outlets || [];
    } catch (err) {
      pduOutlets = [];
      inputError = err.message || 'Could not load outlets.';
    } finally {
      pduLoading = false;
    }
  }

  function outletOwner(outlet, input) {
    if (!outlet.server_power_input) return null;
    if (input && outlet.server_power_input.id === input.id) return null;
    return outlet.server_power_input;
  }

  function openAdd() {
    editingInput = null;
    inputForm = { name: '', description: '', pdu_id: '', pdu_outlet_id: '' };
    inputError = '';
    selectedPduId = '';
    pduOutlets = [];
    showInputModal = true;
  }

  function openEdit(input) {
    editingInput = input;
    const pduId = input.pdu ? String(input.pdu.id) : '';
    inputForm = {
      name: input.name,
      description: input.description || '',
      pdu_id: pduId,
      pdu_outlet_id: input.outlet ? String(input.outlet.id) : ''
    };
    inputError = '';
    selectedPduId = pduId;
    showInputModal = true;
    loadPduOutlets(pduId);
  }

  function closeInputModal() {
    showInputModal = false;
    editingInput = null;
  }

  async function onPduChange() {
    inputForm.pdu_outlet_id = '';
    selectedPduId = inputForm.pdu_id;
    await loadPduOutlets(inputForm.pdu_id);
  }

  async function saveInput() {
    inputError = '';
    if (!inputForm.name.trim()) {
      inputError = 'Enter a name for this power input.';
      return;
    }
    saving = true;
    const body = {
      name: inputForm.name.trim(),
      description: inputForm.description.trim() || null,
      pdu_outlet_id: inputForm.pdu_outlet_id ? Number(inputForm.pdu_outlet_id) : null
    };
    try {
      if (editingInput) {
        await updateServerPowerInput(serverId, editingInput.id, body);
      } else {
        await createServerPowerInput(serverId, body);
      }
      closeInputModal();
      await load();
    } catch (err) {
      inputError = err.message || 'Could not save the power input.';
    } finally {
      saving = false;
    }
  }

  async function unplug(input) {
    actionError = '';
    try {
      await updateServerPowerInput(serverId, input.id, { pdu_outlet_id: null });
      await load();
    } catch (err) {
      actionError = err.message || 'Could not unplug the input.';
    }
  }

  async function removeInput(input) {
    if (!confirm(`Remove power input "${input.name}"?`)) return;
    actionError = '';
    try {
      await deleteServerPowerInput(serverId, input.id);
      await load();
    } catch (err) {
      actionError = err.message || 'Could not remove the input.';
    }
  }

  function askAction(input, action) {
    pendingAction = { input, action };
    actionError = '';
  }

  function cancelAction() {
    pendingAction = null;
  }

  async function confirmAction() {
    if (!pendingAction) return;
    const { input, action } = pendingAction;
    actionBusy = true;
    actionError = '';
    try {
      await actionServerPowerInputOutlet(serverId, input.id, action);
      pendingAction = null;
      await load();
    } catch (err) {
      actionError = err.message || 'The outlet action failed.';
    } finally {
      actionBusy = false;
    }
  }

  const ACTION_LABELS = { on: 'On', off: 'Off', cycle: 'Cycle' };

  onMount(() => {
    load();
    loadPdus();
  });
</script>

<div class="power-card">
  {#if loading}
    <p class="muted">Loading power inputs…</p>
  {:else if error}
    <div class="alert alert-danger">{error}</div>
  {:else if data}
    <div class="power-summary">
      {#if data.metered}
        <div>
          <span class="power-label">Total draw</span>
          <strong>{formatWatts(data.total_power_w)}</strong>
          <span class="muted">{formatAmps(data.total_current_a)}</span>
          {#if !data.fully_metered}
            <span class="muted small">(some inputs not metered)</span>
          {/if}
        </div>
      {:else}
        <div class="muted">Not metered</div>
      {/if}
      <div>
        <button type="button" class="btn btn-primary btn-sm" on:click={openAdd}>Add power input</button>
      </div>
    </div>

    {#if actionError}
      <div class="alert alert-danger">{actionError}</div>
    {/if}

    {#if data.inputs.length === 0}
      <p class="muted">No power inputs. Add one for each PSU, then link it to a PDU outlet.</p>
    {:else}
      <div class="admin-data-table">
        <table>
          <thead>
            <tr>
              <th>Input</th>
              <th>PDU</th>
              <th>Outlet</th>
              <th>Feeds</th>
              <th>State</th>
              <th>Draw</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {#each data.inputs as input (input.id)}
              <tr>
                <td>
                  <strong>{input.name}</strong>
                  {#if input.description}<div class="muted small">{input.description}</div>{/if}
                </td>
                <td>
                  {#if input.pdu}
                    {input.pdu.name}
                    {#if !input.pdu.enabled}<span class="muted small">(disabled)</span>{/if}
                  {:else}
                    <span class="muted">Not plugged in</span>
                  {/if}
                </td>
                <td>
                  {#if input.outlet}
                    {input.outlet.name}
                    {#if input.outlet.bank}<span class="muted small">({input.outlet.bank})</span>{/if}
                  {:else}
                    <span class="muted">-</span>
                  {/if}
                </td>
                <td>
                  {#if input.feeds && input.feeds.length}
                    {#each input.feeds as feed (feed.id)}
                      <div class="small">
                        {feed.name}{#if feed.inlet_name} <span class="muted">({feed.inlet_name})</span>{/if}
                        {#if feed.active === false}<span class="muted">inactive</span>{/if}
                      </div>
                    {/each}
                  {:else}
                    <span class="muted">-</span>
                  {/if}
                </td>
                <td>
                  {#if input.outlet && input.outlet.state}
                    <span class="status-badge {input.outlet.state === 'on' ? 'status-active' : 'status-inactive'}">{input.outlet.state}</span>
                  {:else}
                    <span class="muted">-</span>
                  {/if}
                </td>
                <td>
                  {#if data.metered && input.outlet && input.outlet.power_w !== null && input.outlet.power_w !== undefined}
                    {formatWatts(input.outlet.power_w)}
                    <div class="muted small">{formatAmps(input.outlet.current_a)}</div>
                  {:else}
                    <span class="muted">{input.outlet ? 'Not metered' : '-'}</span>
                  {/if}
                </td>
                <td class="actions">
                  {#if input.can_control}
                    <button type="button" class="btn btn-secondary btn-sm" on:click={() => askAction(input, 'on')}>On</button>
                    <button type="button" class="btn btn-secondary btn-sm" on:click={() => askAction(input, 'off')}>Off</button>
                    <button type="button" class="btn btn-secondary btn-sm" on:click={() => askAction(input, 'cycle')}>Cycle</button>
                  {:else if input.control_blocked_reason}
                    <span class="muted small" title={input.control_blocked_reason}>Control unavailable</span>
                  {/if}
                  <button type="button" class="btn btn-secondary btn-sm" on:click={() => openEdit(input)}>
                    {input.outlet ? 'Change outlet' : 'Link outlet'}
                  </button>
                  {#if input.outlet}
                    <button type="button" class="btn btn-secondary btn-sm" on:click={() => unplug(input)}>Unplug</button>
                  {/if}
                  <button type="button" class="btn btn-secondary btn-sm" on:click={() => removeInput(input)}>Remove</button>
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  {/if}
</div>

{#if showInputModal}
  <div class="modal-overlay" on:click|self={closeInputModal} role="presentation">
    <div class="modal-content" role="dialog" aria-modal="true">
      <div class="modal-header">
        <h3>{editingInput ? 'Edit power input' : 'Add power input'}</h3>
        <button type="button" class="btn-icon-only" aria-label="Close" on:click={closeInputModal}>&times;</button>
      </div>
      <div class="modal-body">
        {#if inputError}
          <div class="alert alert-danger">{inputError}</div>
        {/if}
        <div class="form-group">
          <label for="pi-name">Name</label>
          <input id="pi-name" type="text" bind:value={inputForm.name} placeholder="PSU1" />
        </div>
        <div class="form-group">
          <label for="pi-desc">Description (optional)</label>
          <input id="pi-desc" type="text" bind:value={inputForm.description} />
        </div>
        <div class="form-group">
          <label for="pi-pdu">PDU</label>
          <select id="pi-pdu" bind:value={inputForm.pdu_id} on:change={onPduChange}>
            <option value="">Not plugged in</option>
            {#each pdus as pdu (pdu.id)}
              <option value={String(pdu.id)}>{pdu.name}{pdu.rack_name ? ` (${pdu.rack_name})` : ''}</option>
            {/each}
          </select>
          {#if pdus.length === 0}
            <div class="field-help">No PDUs in this server's location yet.</div>
          {/if}
        </div>
        {#if inputForm.pdu_id}
          <div class="form-group">
            <label for="pi-outlet">Outlet</label>
            {#if pduLoading}
              <p class="muted small">Loading outlets…</p>
            {:else}
              <select id="pi-outlet" bind:value={inputForm.pdu_outlet_id}>
                <option value="">No outlet</option>
                {#each pduOutlets as outlet (outlet.id)}
                  {@const owner = outletOwner(outlet, editingInput)}
                  <option value={String(outlet.id)} disabled={!!owner}>
                    {outlet.name}{outlet.bank ? ` (${outlet.bank})` : ''}{owner ? ` - in use by ${owner.server_name} ${owner.name}` : ''}
                  </option>
                {/each}
              </select>
            {/if}
          </div>
        {/if}
      </div>
      <div class="modal-footer">
        <button type="button" class="btn btn-secondary" on:click={closeInputModal}>Cancel</button>
        <button type="button" class="btn btn-primary" on:click={saveInput} disabled={saving}>
          {saving ? 'Saving…' : 'Save'}
        </button>
      </div>
    </div>
  </div>
{/if}

{#if pendingAction}
  <div class="modal-overlay" role="presentation">
    <div class="modal-content" role="alertdialog" aria-modal="true">
      <div class="modal-header">
        <h3>{ACTION_LABELS[pendingAction.action]} outlet?</h3>
      </div>
      <div class="modal-body">
        <p>
          This will switch power for <strong>{serverName}</strong> ({pendingAction.input.name}) through
          PDU <strong>{pendingAction.input.pdu ? pendingAction.input.pdu.name : '-'}</strong>,
          outlet <strong>{pendingAction.input.outlet ? pendingAction.input.outlet.name : '-'}</strong>.
        </p>
        {#if actionError}
          <div class="alert alert-danger">{actionError}</div>
        {/if}
      </div>
      <div class="modal-footer">
        <button type="button" class="btn btn-secondary" on:click={cancelAction} disabled={actionBusy}>Cancel</button>
        <button type="button" class="btn btn-danger" on:click={confirmAction} disabled={actionBusy}>
          {actionBusy ? 'Working…' : ACTION_LABELS[pendingAction.action]}
        </button>
      </div>
    </div>
  </div>
{/if}

<style>
  .power-card {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
  }
  .power-summary {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 1rem;
    flex-wrap: wrap;
  }
  .power-label {
    display: block;
    font-size: 0.8rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-secondary);
  }
  .actions {
    white-space: nowrap;
  }
  .actions .btn {
    margin-right: 0.25rem;
  }
  .muted {
    color: var(--text-secondary);
  }
  .small {
    font-size: 0.85em;
  }
</style>
