<script>
  import PageHeader from './PageHeader.svelte';
  import {
    getPdu,
    getPduHistory,
    getRacks,
    getPowerFeeds,
    pollPdu,
    regeneratePduPorts,
    updatePduInlet,
    updatePduOutlet,
    actionPduOutlet
  } from '../lib/api.js';
  import { navigate } from '../lib/router.js';
  import { formatWatts, formatAmps, formatRelativeTime } from '../lib/power.js';
  import { onMount } from 'svelte';
  import PowerHistoryPanel from './PowerHistoryPanel.svelte';
  import { buildPduCharts } from '../lib/powerCharts.js';

  export let pduId;
  export let onBack;

  const ACTION_LABELS = { on: 'On', off: 'Off', cycle: 'Cycle' };

  let pdu = null;
  let racks = [];
  let feeds = [];
  let loading = true;
  let error = null;
  let busy = false;
  let message = null;
  let messageIsError = false;
  let pendingAction = null;
  let actionSubmitting = false;
  let actionError = null;

  onMount(async () => {
    await loadPdu();
  });

  async function loadPdu() {
    try {
      loading = true;
      error = null;
      pdu = await getPdu(pduId);
      await loadFeeds();
    } catch (err) {
      error = err.message;
      console.error('Failed to load PDU:', err);
    } finally {
      loading = false;
    }
  }

  async function loadFeeds() {
    if (!pdu.location_id) {
      racks = [];
      feeds = [];
      return;
    }
    racks = await getRacks(pdu.location_id);
    feeds = (await Promise.all(racks.map((rack) => getPowerFeeds(rack.id)))).flat();
  }

  async function refreshPdu() {
    pdu = await getPdu(pduId);
  }

  function showMessage(text, isError) {
    message = text;
    messageIsError = isError;
  }

  function rackName(rackId) {
    return racks.find((rack) => rack.id === rackId)?.name || 'Unknown rack';
  }

  function feedLabel(feed) {
    return `${rackName(feed.rack_id)} / ${feed.name}`;
  }

  function findFeed(value) {
    return feeds.find((feed) => String(feed.id) === value) || null;
  }

  function bankPercent(bank) {
    if (!bank.rated_current_a) return null;
    return Math.min(100, ((bank.current_a || 0) / bank.rated_current_a) * 100);
  }

  function bankTone(percent) {
    if (percent === null) return 'ok';
    if (percent >= 100) return 'danger';
    if (percent >= 80) return 'warning';
    return 'ok';
  }

  async function handlePoll() {
    busy = true;
    message = null;
    try {
      const result = await pollPdu(pduId);
      await refreshPdu();
      showMessage(result.message || (result.success ? 'Poll complete' : 'Poll failed'), !result.success);
    } catch (err) {
      showMessage(err.message, true);
    } finally {
      busy = false;
    }
  }

  async function handleRegenerate() {
    busy = true;
    message = null;
    try {
      const result = await regeneratePduPorts(pduId);
      await refreshPdu();
      showMessage(
        `Ports regenerated: ${result.created} created, ${result.updated} updated, ${result.deleted} deleted, ${result.kept_linked} kept linked.`,
        !result.success
      );
    } catch (err) {
      showMessage(err.message, true);
    } finally {
      busy = false;
    }
  }

  async function handleInletFeed(inlet, event) {
    const feed = findFeed(event.target.value);
    try {
      pdu = await updatePduInlet(pduId, inlet.id, { power_feed_id: feed ? feed.id : null });
      showMessage(null, false);
    } catch (err) {
      showMessage(err.message, true);
      await refreshPdu();
    }
  }

  async function saveOutlet(outlet, data) {
    try {
      pdu = await updatePduOutlet(pduId, outlet.id, data);
    } catch (err) {
      showMessage(err.message, true);
      await refreshPdu();
    }
  }

  function requestOutletAction(outlet, action) {
    pendingAction = { outlet, action };
    actionError = null;
  }

  function cancelOutletAction() {
    pendingAction = null;
    actionError = null;
  }

  async function confirmOutletAction() {
    if (!pendingAction) return;
    const { outlet, action } = pendingAction;
    actionSubmitting = true;
    actionError = null;
    try {
      await actionPduOutlet(pduId, outlet.id, action);
      pendingAction = null;
      await refreshPdu();
      showMessage(`Outlet ${outlet.name || outlet.position} set to ${ACTION_LABELS[action].toLowerCase()}`, false);
    } catch (err) {
      actionError = err.message;
    } finally {
      actionSubmitting = false;
    }
  }

  function outletStateLabel(outlet) {
    if (outlet.state === 'on') return 'On';
    if (outlet.state === 'off') return 'Off';
    return 'Unknown';
  }

  function controlTitle(outlet) {
    if (outlet.can_control) return '';
    return outlet.control_blocked_reason || 'Control is not available for this outlet';
  }
</script>

<PageHeader title={pdu ? `PDU: ${pdu.name}` : 'PDU Details'}>
  <svelte:fragment slot="actions">
    {#if pdu}
      <button class="btn-secondary" on:click={handlePoll} disabled={busy}>Poll now</button>
      <button class="btn-secondary" on:click={handleRegenerate} disabled={busy}>Regenerate ports</button>
    {/if}
    <button class="btn-secondary" on:click={onBack}>Back to PDUs</button>
  </svelte:fragment>
</PageHeader>

{#if loading}
  <div class="loading">Loading PDU...</div>
{:else if error}
  <div class="error-message">{error}</div>
{:else if pdu}
  {#if message}
    <div class={messageIsError ? 'error-message' : 'success-message'}>{message}</div>
  {/if}

  <div class="card">
    <div class="card-header">
      <h3>Summary</h3>
    </div>
    <div class="card-body">
      <dl class="summary-grid">
        <dt>Model</dt>
        <dd>{pdu.model || 'N/A'}</dd>
        <dt>Serial number</dt>
        <dd>{pdu.serial_number || 'N/A'}</dd>
        <dt>Firmware</dt>
        <dd>{pdu.firmware_version || 'N/A'}</dd>
        <dt>Plugin</dt>
        <dd>{pdu.plugin_name}</dd>
        <dt>Total power</dt>
        <dd>
          {formatWatts(pdu.last_readings?.totals?.power_w ?? pdu.power_w)}
          {#if pdu.last_readings?.power_estimated}<span class="muted">(estimated)</span>{/if}
        </dd>
        <dt>Total current</dt>
        <dd>{formatAmps(pdu.last_readings?.totals?.current_a ?? pdu.current_a)}</dd>
        <dt>Last polled</dt>
        <dd>{formatRelativeTime(pdu.last_polled_at)}</dd>
        <dt>Status</dt>
        <dd>
          <span class="status-badge" class:enabled={pdu.enabled} class:disabled={!pdu.enabled}>
            {pdu.enabled ? 'Enabled' : 'Disabled'}
          </span>
        </dd>
      </dl>
      {#if pdu.last_poll_error}
        <div class="error-message poll-error">Last poll failed: {pdu.last_poll_error}</div>
      {/if}
    </div>
  </div>

  <div class="card">
    <div class="card-body">
      <PowerHistoryPanel
        title="Power history"
        loader={(hours) => getPduHistory(pdu.id, hours)}
        build={buildPduCharts}
        reloadKey={pdu.last_polled_at}
      />
    </div>
  </div>

  {#if pdu.last_readings?.banks?.length}
    <div class="card">
      <div class="card-header">
        <h3>Banks</h3>
      </div>
      <div class="card-body">
        {#each pdu.last_readings.banks as bank}
          {@const percent = bankPercent(bank)}
          <div class="bank-row">
            <div class="bank-label">
              <span>{bank.name}</span>
              <span class="muted">
                {formatAmps(bank.current_a)}{bank.rated_current_a ? ` of ${formatAmps(bank.rated_current_a)} rated` : ''}
                · {formatWatts(bank.power_w)}
              </span>
            </div>
            {#if percent !== null}
              <div class="bar">
                <div class="bar-fill {bankTone(percent)}" style="width: {percent}%"></div>
              </div>
            {/if}
          </div>
        {/each}
      </div>
    </div>
  {/if}

  {#if pdu.last_readings?.sensors?.length}
    <div class="card">
      <div class="card-header">
        <h3>Sensors</h3>
      </div>
      <div class="card-body">
        <div class="table-wrap">
          <table class="admin-data-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Type</th>
                <th>Value</th>
              </tr>
            </thead>
            <tbody>
              {#each pdu.last_readings.sensors as sensor}
                <tr>
                  <td>{sensor.name}</td>
                  <td>{sensor.type || 'N/A'}</td>
                  <td>{sensor.value ?? 'N/A'} {sensor.unit || ''}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  {/if}

  <div class="card">
    <div class="card-header">
      <h3>Inlets</h3>
    </div>
    <div class="card-body">
      {#if pdu.inlets?.length}
        <div class="table-wrap">
          <table class="admin-data-table">
            <thead>
              <tr>
                <th>Inlet</th>
                <th>Power feed</th>
                <th>Voltage</th>
                <th>Current</th>
                <th>Power</th>
                <th>Apparent</th>
                <th>Energy</th>
                <th>Active</th>
              </tr>
            </thead>
            <tbody>
              {#each pdu.inlets as inlet}
                <tr>
                  <td>{inlet.name || `Inlet ${inlet.position}`}</td>
                  <td>
                    <select
                      class="form-select-sm"
                      value={inlet.power_feed_id ?? ''}
                      on:change={(event) => handleInletFeed(inlet, event)}
                    >
                      <option value="">Not linked</option>
                      {#each feeds as feed}
                        <option value={feed.id}>{feedLabel(feed)}</option>
                      {/each}
                      {#if inlet.power_feed_id && !findFeed(String(inlet.power_feed_id))}
                        <option value={inlet.power_feed_id}>{inlet.power_feed_name}</option>
                      {/if}
                    </select>
                  </td>
                  <td>{inlet.voltage_v != null ? `${inlet.voltage_v} V` : 'N/A'}</td>
                  <td>{formatAmps(inlet.current_a)}</td>
                  <td>{formatWatts(inlet.power_w)}</td>
                  <td>{inlet.apparent_power_va != null ? `${Math.round(inlet.apparent_power_va)} VA` : 'N/A'}</td>
                  <td>{inlet.energy_kwh != null ? `${Number(inlet.energy_kwh).toFixed(2)} kWh` : 'N/A'}</td>
                  <td>
                    {#if inlet.active === true}
                      <span class="status-badge enabled">Active</span>
                    {:else if inlet.active === false}
                      <span class="status-badge disabled">Standby</span>
                    {:else}
                      <span class="muted">N/A</span>
                    {/if}
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {:else}
        <p class="muted">No inlets. Use Regenerate ports to create them.</p>
      {/if}
    </div>
  </div>

  <div class="card">
    <div class="card-header">
      <h3>Outlets</h3>
    </div>
    <div class="card-body">
      {#if pdu.outlets?.length}
        <div class="table-wrap">
          <table class="admin-data-table">
            <thead>
              <tr>
                <th>#</th>
                <th>Name</th>
                <th>Bank</th>
                <th>State</th>
                {#if pdu.outlet_metering}
                  <th>Current</th>
                  <th>Power</th>
                {/if}
                <th>Server / PSU</th>
                <th>Locked</th>
                {#if pdu.outlet_switching}
                  <th>Control</th>
                {/if}
              </tr>
            </thead>
            <tbody>
              {#each pdu.outlets as outlet}
                <tr>
                  <td>{outlet.position}</td>
                  <td>
                    <input
                      class="form-input-sm"
                      type="text"
                      value={outlet.name ?? ''}
                      on:change={(event) => saveOutlet(outlet, { name: event.target.value })}
                    />
                  </td>
                  <td>
                    <input
                      class="form-input-sm"
                      type="text"
                      value={outlet.bank ?? ''}
                      on:change={(event) => saveOutlet(outlet, { bank: event.target.value })}
                    />
                  </td>
                  <td>
                    <span
                      class="status-badge"
                      class:enabled={outlet.state === 'on'}
                      class:disabled={outlet.state === 'off'}
                    >
                      {outletStateLabel(outlet)}
                    </span>
                  </td>
                  {#if pdu.outlet_metering}
                    <td>{formatAmps(outlet.current_a)}</td>
                    <td>{formatWatts(outlet.power_w)}</td>
                  {/if}
                  <td>
                    {#if outlet.server_power_input}
                      <a
                        href="/admin/servers/{outlet.server_power_input.server_id}"
                        on:click|preventDefault={() => navigate(`/admin/servers/${outlet.server_power_input.server_id}`)}
                      >
                        {outlet.server_power_input.server_name}
                      </a>
                      <span class="muted">/ {outlet.server_power_input.name}</span>
                    {:else}
                      <span class="muted">Not in use</span>
                    {/if}
                  </td>
                  <td>
                    <input
                      type="checkbox"
                      checked={outlet.locked}
                      title="Lock this outlet against changes from Rackflow"
                      on:change={(event) => saveOutlet(outlet, { locked: event.target.checked })}
                    />
                  </td>
                  {#if pdu.outlet_switching}
                    <td>
                      {#if outlet.can_control}
                        <div class="control-buttons">
                          <button class="btn-secondary btn-small" on:click={() => requestOutletAction(outlet, 'on')}>On</button>
                          <button class="btn-secondary btn-small" on:click={() => requestOutletAction(outlet, 'off')}>Off</button>
                          <button class="btn-secondary btn-small" on:click={() => requestOutletAction(outlet, 'cycle')}>Cycle</button>
                        </div>
                      {:else}
                        <span class="muted" title={controlTitle(outlet)}>
                          {outlet.locked ? 'Locked' : 'Blocked'}
                        </span>
                      {/if}
                    </td>
                  {/if}
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {:else}
        <p class="muted">No outlets. Use Regenerate ports to create them.</p>
      {/if}
    </div>
  </div>
{/if}

{#if pendingAction}
  <div class="confirm-backdrop" on:click|self={cancelOutletAction} role="presentation">
    <div class="confirm-dialog" role="dialog" aria-modal="true">
      <h3>{ACTION_LABELS[pendingAction.action]} outlet {pendingAction.outlet.position}?</h3>
      <p>
        This will switch power on {pdu?.name} for outlet {pendingAction.outlet.name || pendingAction.outlet.position}
        {#if pendingAction.outlet.server_power_input}
          (feeding {pendingAction.outlet.server_power_input.server_name}, {pendingAction.outlet.server_power_input.name})
        {/if}.
      </p>
      {#if actionError}
        <div class="error-message">{actionError}</div>
      {/if}
      <div class="confirm-actions">
        <button class="btn-secondary" on:click={cancelOutletAction} disabled={actionSubmitting}>Cancel</button>
        <button class="btn-primary" on:click={confirmOutletAction} disabled={actionSubmitting}>
          {actionSubmitting ? 'Working...' : ACTION_LABELS[pendingAction.action]}
        </button>
      </div>
    </div>
  </div>
{/if}

<style>
  .card {
    background: var(--bg-secondary);
    border: 1px solid var(--border-color);
    border-radius: 0.5rem;
    margin-bottom: 1.5rem;
  }

  .card-header {
    padding: 0.75rem 1rem;
    border-bottom: 1px solid var(--border-color);
  }

  .card-header h3 {
    margin: 0;
    font-size: 1.1rem;
  }

  .card-body {
    padding: 1rem;
  }

  .summary-grid {
    display: grid;
    grid-template-columns: max-content 1fr;
    gap: 0.4rem 1.5rem;
    margin: 0;
  }

  .summary-grid dt {
    font-weight: 600;
  }

  .summary-grid dd {
    margin: 0;
  }

  .muted {
    color: var(--text-secondary);
    font-size: 0.9em;
  }

  .poll-error {
    margin-top: 1rem;
  }

  .loading,
  .error-message,
  .success-message {
    padding: 0.75rem;
    border-radius: 0.25rem;
    margin-bottom: 1rem;
  }

  .error-message {
    background: var(--danger-bg);
    color: var(--danger-text);
  }

  .success-message {
    background: var(--success-bg);
    color: var(--success-text);
  }

  .bank-row + .bank-row {
    margin-top: 0.75rem;
  }

  .bank-label {
    display: flex;
    justify-content: space-between;
    gap: 1rem;
    margin-bottom: 0.25rem;
  }

  .bar {
    height: 0.6rem;
    background: var(--bg-tertiary);
    border-radius: 0.3rem;
    overflow: hidden;
  }

  .bar-fill {
    height: 100%;
    background: #198754;
  }

  .bar-fill.warning {
    background: #ffc107;
  }

  .bar-fill.danger {
    background: #dc3545;
  }

  .status-badge {
    display: inline-block;
    padding: 0.15rem 0.5rem;
    border-radius: 0.25rem;
    font-size: 0.8rem;
    background: var(--bg-tertiary);
  }

  .status-badge.enabled {
    background: var(--success-bg);
    color: var(--success-text);
  }

  .status-badge.disabled {
    background: var(--bg-tertiary);
    color: var(--text-secondary);
  }

  .table-wrap {
    overflow-x: auto;
  }

  .admin-data-table {
    width: 100%;
    border-collapse: collapse;
  }

  .admin-data-table th,
  .admin-data-table td {
    padding: 0.5rem;
    border-bottom: 1px solid var(--border-color);
    text-align: left;
    vertical-align: middle;
  }

  .form-select-sm,
  .form-input-sm {
    min-width: 10rem;
    padding: 0.25rem 0.4rem;
    border: 1px solid var(--border-color);
    border-radius: 0.25rem;
    background: var(--bg-primary);
    color: inherit;
  }

  .control-buttons {
    display: flex;
    gap: 0.35rem;
  }

  .btn-small {
    padding: 0.2rem 0.5rem;
    font-size: 0.85rem;
  }

  .btn-primary,
  .btn-secondary {
    padding: 0.4rem 0.9rem;
    border-radius: 0.25rem;
    border: 1px solid transparent;
    cursor: pointer;
  }

  .btn-primary {
    background: var(--accent-color);
    color: #fff;
  }

  .btn-primary:hover {
    background: var(--accent-dark);
  }

  .btn-secondary {
    background: var(--bg-primary);
    color: inherit;
    border-color: var(--border-color);
  }

  .btn-primary:disabled,
  .btn-secondary:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .confirm-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.5);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1100;
  }

  .confirm-dialog {
    background: var(--bg-secondary);
    border-radius: 0.5rem;
    padding: 1.25rem;
    max-width: 30rem;
    width: 100%;
  }

  .confirm-dialog h3 {
    margin-top: 0;
  }

  .confirm-actions {
    display: flex;
    justify-content: flex-end;
    gap: 0.5rem;
    margin-top: 1rem;
  }
</style>
