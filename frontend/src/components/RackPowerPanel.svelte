<script>
  import { onMount } from 'svelte';
  import { getRackPower, getPowerFeedHistory, getRackPowerHistory, createPowerFeed, updatePowerFeed, deletePowerFeed } from '../lib/api.js';
  import {
    formatWatts,
    formatAmps,
    formatRelativeTime,
    feedUsageTone,
    feedUsagePercent,
    feedLimitPercent
  } from '../lib/power.js';

  import PowerHistoryPanel from './PowerHistoryPanel.svelte';
  import { buildFeedCharts, buildRackCharts } from '../lib/powerCharts.js';

  export let rackId;
  let openHistory = {};
  let showRackHistory = false;

  const phaseLabels = { single: 'Single phase', three: 'Three phase' };

  let power = null;
  let loading = true;
  let error = null;

  let showModal = false;
  let editingFeed = null;
  let formData = emptyForm();
  let formError = null;
  let submitting = false;

  onMount(loadPower);

  function emptyForm() {
    return {
      name: '',
      voltage: 208,
      amperage: 30,
      phase: 'single',
      max_utilisation_percent: 80,
      description: ''
    };
  }

  async function loadPower() {
    try {
      loading = true;
      error = null;
      power = await getRackPower(rackId);
    } catch (err) {
      error = err.message;
      console.error('Failed to load rack power:', err);
    } finally {
      loading = false;
    }
  }

  function openAddModal() {
    editingFeed = null;
    formData = emptyForm();
    formError = null;
    showModal = true;
  }

  function openEditModal(feed) {
    editingFeed = feed;
    formData = {
      name: feed.name,
      voltage: feed.voltage,
      amperage: feed.amperage,
      phase: feed.phase,
      max_utilisation_percent: feed.max_utilisation_percent,
      description: feed.description || ''
    };
    formError = null;
    showModal = true;
  }

  function closeModal() {
    showModal = false;
    editingFeed = null;
  }

  async function handleSubmit() {
    submitting = true;
    formError = null;
    const payload = {
      ...formData,
      voltage: Number(formData.voltage),
      amperage: Number(formData.amperage),
      max_utilisation_percent: Number(formData.max_utilisation_percent),
      description: formData.description || null
    };
    try {
      if (editingFeed) {
        await updatePowerFeed(editingFeed.id, payload);
      } else {
        await createPowerFeed({ ...payload, rack_id: rackId });
      }
      closeModal();
      await loadPower();
    } catch (err) {
      formError = err.message;
    } finally {
      submitting = false;
    }
  }

  async function handleDelete(feed) {
    if (!window.confirm(`Delete power feed "${feed.name}"? PDU inlets linked to it will be unlinked.`)) return;
    try {
      await deletePowerFeed(feed.id);
      await loadPower();
    } catch (err) {
      error = err.message;
    }
  }

  function utilisationLabel(feed) {
    return `${Math.round(feed.utilisation_percent ?? 0)}%`;
  }

  $: feeds = power?.feeds || [];
  $: pdus = power?.pdus || [];
  $: usedPercent = power && power.total_usable_w ? Math.min(100, (power.total_power_w / power.total_usable_w) * 100) : 0;
</script>

<div class="power-panel">
  <div class="power-panel-header">
    <h3>Power</h3>
    <button class="btn-primary" on:click={openAddModal} disabled={loading || !!error && !power}>Add feed</button>
  </div>

  {#if loading && !power}
    <div class="loading">Loading power...</div>
  {:else if error && !power}
    <div class="error">Error: {error}</div>
  {:else if power}
    {#if error}
      <div class="form-error">{error}</div>
    {/if}

    <div class="power-totals">
      <div class="total-item">
        <span class="total-label">Rack draw</span>
        <span class="total-value">{formatWatts(power.total_power_w)}</span>
        <span class="total-sub">{formatAmps(power.total_current_a)} total</span>
      </div>
      <div class="total-item">
        <span class="total-label">Usable capacity</span>
        <span class="total-value">{formatWatts(power.total_usable_w)}</span>
        <span class="total-sub">of {formatWatts(power.total_capacity_w)} breaker capacity</span>
      </div>
      <div class="total-item">
        <span class="total-label">Usable used</span>
        <span class="total-value">{Math.round(usedPercent)}%</span>
        <span class="total-sub">rack draw against usable capacity</span>
      </div>
    </div>

    {#if feeds.length === 0}
      <div class="empty-state">No power feeds in this rack yet. Add one to see usage against its breaker.</div>
    {:else}
      <div class="feed-grid">
        {#each feeds as feed (feed.id)}
          {@const tone = feedUsageTone(feed)}
          <div class="feed-card" class:over={feed.over_limit}>
            <div class="feed-header">
              <div>
                <strong class="feed-name">{feed.name}</strong>
                <span class="muted">{feed.voltage} V, {feed.amperage} A, {phaseLabels[feed.phase] || feed.phase}</span>
              </div>
              <div class="feed-actions">
                <button class="btn-secondary btn-small" on:click={() => openEditModal(feed)}>Edit</button>
                <button class="btn-secondary btn-small danger-text" on:click={() => handleDelete(feed)}>Delete</button>
              </div>
            </div>

            {#if feed.description}
              <p class="muted feed-description">{feed.description}</p>
            {/if}

            <div class="usage-bar" title="Breaker {feed.amperage} A, derated limit {feed.usable_current_a} A">
              <div class="usage-fill {tone}" style="width: {feedUsagePercent(feed)}%"></div>
              {#if feed.usable_current_a}
                <div class="usage-limit" style="left: {feedLimitPercent(feed)}%"></div>
              {/if}
            </div>

            <div class="feed-readings">
              <span><strong>{formatAmps(feed.current_a)}</strong> of {feed.amperage} A ({utilisationLabel(feed)})</span>
              <span><strong>{formatWatts(feed.power_w)}</strong> of {formatWatts(feed.usable_w)} usable</span>
            </div>

            {#if feed.over_limit}
              <div class="feed-warning">Above the derated limit of {formatAmps(feed.usable_current_a)} ({feed.max_utilisation_percent}% of breaker).</div>
            {/if}

            <div class="feed-inlets-title">PDU inlets</div>
            <ul class="inlet-list">
              {#each feed.inlets || [] as inlet (inlet.inlet_id)}
                <li>
                  <a href="/admin/pdus/{inlet.pdu_id}">{inlet.pdu_name}</a> / {inlet.inlet_name}
                  <span class="muted">{formatAmps(inlet.current_a)}, {formatWatts(inlet.power_w)}</span>
                  {#if !inlet.active}
                    <span class="tag">standby</span>
                  {/if}
                </li>
              {:else}
                <li class="muted">No PDU inlets drawing from this feed.</li>
              {/each}
            </ul>
            <button
              type="button"
              class="btn-secondary btn-small history-toggle"
              on:click={() => (openHistory = { ...openHistory, [feed.id]: !openHistory[feed.id] })}
            >
              {openHistory[feed.id] ? 'Hide history' : 'Show history'}
            </button>
            {#if openHistory[feed.id]}
              <div class="history-block">
                <PowerHistoryPanel
                  title="{feed.name} history"
                  loader={(hours) => getPowerFeedHistory(feed.id, hours)}
                  build={buildFeedCharts}
                  reloadKey={feed.id}
                />
              </div>
            {/if}
          </div>
        {/each}
      </div>
    {/if}

    <div class="pdu-section-title">PDUs drawing from this rack</div>
    {#if pdus.length === 0}
      <div class="empty-state">No PDUs are in this rack.</div>
    {:else}
      <div class="admin-data-table">
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th>Model</th>
              <th>Power</th>
              <th>Current</th>
              <th>Last polled</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {#each pdus as pdu (pdu.id)}
              <tr>
                <td>
                  <a href="/admin/pdus/{pdu.id}">{pdu.name}</a>
                  {#if !pdu.enabled}<span class="tag">disabled</span>{/if}
                </td>
                <td>{pdu.model || 'N/A'}</td>
                <td>{formatWatts(pdu.power_w)}</td>
                <td>{formatAmps(pdu.current_a)}</td>
                <td>{formatRelativeTime(pdu.last_polled_at)}</td>
                <td>
                  {#if pdu.last_poll_error}
                    <span class="tag danger" title={pdu.last_poll_error}>Poll error</span>
                  {:else}
                    <span class="tag ok">OK</span>
                  {/if}
                  {#if pdu.unlinked_inlets}
                    <span class="tag warn" title="PDU inlets not linked to a power feed">{pdu.unlinked_inlets} unlinked inlet{pdu.unlinked_inlets === 1 ? '' : 's'}</span>
                  {/if}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}

    <button type="button" class="btn-secondary btn-small history-toggle" on:click={() => (showRackHistory = !showRackHistory)}>
      {showRackHistory ? 'Hide rack power history' : 'Show rack power history'}
    </button>
    {#if showRackHistory}
      <div class="history-block">
        <PowerHistoryPanel
          title="Rack power history"
          loader={(hours) => getRackPowerHistory(rackId, hours)}
          build={buildRackCharts}
          reloadKey={rackId}
        />
      </div>
    {/if}
  {/if}
</div>

{#if showModal}
  <div class="modal-overlay" role="presentation" on:click|self={closeModal} on:keydown={(e) => e.key === 'Escape' && closeModal()}>
    <div class="modal-content" role="dialog" aria-modal="true">
      <div class="modal-header">
        <h3>{editingFeed ? 'Edit power feed' : 'Add power feed'}</h3>
      </div>
      <form on:submit|preventDefault={handleSubmit}>
        <div class="modal-body">
          {#if formError}
            <div class="form-error">{formError}</div>
          {/if}
          <div class="form-group">
            <label for="feed-name">Name <span class="required">*</span></label>
            <input id="feed-name" type="text" maxlength="64" bind:value={formData.name} required />
          </div>
          <div class="form-row">
            <div class="form-group">
              <label for="feed-voltage">Voltage (V)</label>
              <input id="feed-voltage" type="number" min="1" max="1000" bind:value={formData.voltage} required />
            </div>
            <div class="form-group">
              <label for="feed-amperage">Breaker (A)</label>
              <input id="feed-amperage" type="number" min="1" max="1000" bind:value={formData.amperage} required />
            </div>
          </div>
          <div class="form-row">
            <div class="form-group">
              <label for="feed-phase">Phase</label>
              <select id="feed-phase" bind:value={formData.phase}>
                <option value="single">Single phase</option>
                <option value="three">Three phase</option>
              </select>
            </div>
            <div class="form-group">
              <label for="feed-utilisation">Max utilisation (%)</label>
              <input id="feed-utilisation" type="number" min="1" max="100" bind:value={formData.max_utilisation_percent} required />
              <small class="field-help">Derated limit: usage above this share of the breaker is flagged.</small>
            </div>
          </div>
          <div class="form-group">
            <label for="feed-description">Description</label>
            <textarea id="feed-description" rows="2" bind:value={formData.description}></textarea>
          </div>
        </div>
        <div class="modal-footer">
          <button type="button" class="btn-secondary" on:click={closeModal}>Cancel</button>
          <button type="submit" class="btn-primary" disabled={submitting}>
            {submitting ? 'Saving...' : 'Save'}
          </button>
        </div>
      </form>
    </div>
  </div>
{/if}

<style>
  .history-toggle {
    margin-top: 10px;
  }

  .history-block {
    margin-top: 12px;
    padding-top: 12px;
    border-top: 1px solid var(--border-color);
  }

  .power-panel {
    margin-top: 24px;
    padding: 20px;
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: 12px;
  }

  .power-panel-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 16px;
  }

  .power-panel-header h3 {
    margin: 0;
    font-size: 20px;
    font-weight: 700;
    color: var(--text-primary);
  }

  .power-totals {
    display: flex;
    gap: 32px;
    flex-wrap: wrap;
    margin-bottom: 20px;
  }

  .total-item {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .total-label {
    font-size: 12px;
    color: var(--text-secondary);
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }

  .total-value {
    font-size: 20px;
    font-weight: 700;
    color: var(--text-primary);
  }

  .total-sub {
    font-size: 12px;
    color: var(--text-secondary);
  }

  .feed-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
    gap: 16px;
    margin-bottom: 24px;
  }

  .feed-card {
    padding: 16px;
    border: 1px solid var(--border-color);
    border-radius: 10px;
    background: var(--bg-secondary);
  }

  .feed-card.over {
    border-color: var(--danger-color);
  }

  .feed-header {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    align-items: flex-start;
  }

  .feed-name {
    display: block;
    font-size: 16px;
    color: var(--text-primary);
  }

  .feed-actions {
    display: flex;
    gap: 6px;
    flex-shrink: 0;
  }

  .btn-small {
    padding: 4px 10px;
    font-size: 13px;
  }

  .danger-text {
    color: var(--danger-color);
  }

  .feed-description {
    margin: 6px 0 0 0;
  }

  .usage-bar {
    position: relative;
    height: 14px;
    margin: 12px 0 6px 0;
    background: var(--bg-tertiary);
    border-radius: 7px;
    overflow: hidden;
  }

  .usage-fill {
    height: 100%;
    border-radius: 7px;
    transition: width 0.3s ease;
  }

  .usage-fill.ok {
    background: var(--success-color);
  }

  .usage-fill.warning {
    background: var(--warning-color);
  }

  .usage-fill.danger {
    background: var(--danger-color);
  }

  .usage-limit {
    position: absolute;
    top: 0;
    bottom: 0;
    width: 2px;
    background: var(--text-primary);
    opacity: 0.6;
  }

  .feed-readings {
    display: flex;
    flex-direction: column;
    gap: 2px;
    font-size: 13px;
    color: var(--text-secondary);
  }

  .feed-warning {
    margin-top: 8px;
    font-size: 13px;
    color: var(--danger-color);
  }

  .feed-inlets-title,
  .pdu-section-title {
    margin: 14px 0 6px 0;
    font-size: 12px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: var(--text-secondary);
  }

  .pdu-section-title {
    margin-top: 0;
  }

  .inlet-list {
    margin: 0;
    padding-left: 18px;
    font-size: 13px;
    color: var(--text-primary);
  }

  .muted {
    color: var(--text-secondary);
    font-size: 0.9em;
  }

  .tag {
    display: inline-block;
    margin-left: 6px;
    padding: 1px 8px;
    border-radius: 10px;
    font-size: 11px;
    font-weight: 600;
    background: var(--bg-tertiary);
    color: var(--text-secondary);
  }

  .tag.ok {
    background: var(--success-bg);
    color: var(--success-text);
  }

  .tag.danger {
    background: var(--danger-bg);
    color: var(--danger-text);
  }

  .tag.warn {
    background: var(--warning-bg);
    color: var(--warning-text);
  }

  .loading,
  .empty-state {
    text-align: center;
    padding: 24px;
    color: var(--text-secondary);
  }

  .error {
    color: var(--danger-color);
    padding: 12px 0;
  }

</style>
