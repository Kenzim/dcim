<script>
  /**
   * Config-driven component for capabilities with state + action buttons.
   * Renders a card with current state (from state_action) and action buttons.
   */
  import { callServerPluginAction } from '../../lib/api.js';

  export let serverId;
  export let capability; // { id, display_name, description, state_action, actions }
  export let serverName = '';

  let state = null;
  let loadingState = true;
  let actionsInProgress = {};

  async function loadState() {
    if (!capability.state_action) return;
    loadingState = true;
    try {
      const result = await callServerPluginAction(serverId, capability.state_action);
      state = result.power_state ?? result.state ?? result;
    } catch (err) {
      console.error('Failed to load state:', err);
      state = 'unknown';
    } finally {
      loadingState = false;
    }
  }

  async function runAction(action) {
    const confirmMsg = action.confirm || `${action.label} server "${serverName}"?`;
    if (action.confirm !== false && !confirm(confirmMsg)) return;
    actionsInProgress = { ...actionsInProgress, [action.method]: true };
    try {
      await callServerPluginAction(serverId, action.method);
      loadState();
    } catch (err) {
      alert(`Failed: ${err.message}`);
    } finally {
      actionsInProgress = { ...actionsInProgress, [action.method]: false };
    }
  }

  loadState();
</script>

<div class="power-card">
  <div class="power-card-header">
    <h3 class="power-card-title">{capability.display_name}</h3>
    {#if capability.description}
      <p class="power-card-desc">{capability.description}</p>
    {/if}
  </div>
  <div class="power-card-body">
    {#if capability.state_action}
      <div class="state-section">
        <span class="state-label">Current state</span>
        <div class="state-value-row">
          <span
            class="state-badge"
            class:state-on={state === 'on'}
            class:state-off={state === 'off'}
            class:state-unknown={!state || state === 'unknown' || loadingState}
          >
            {loadingState ? '…' : (state === 'on' ? 'On' : state === 'off' ? 'Off' : (state || 'Unknown'))}
          </span>
          <button type="button" class="btn-refresh" on:click={loadState} title="Refresh state" disabled={loadingState}>
            <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" width="14" height="14">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
            </svg>
          </button>
        </div>
      </div>
    {/if}
    <div class="action-section">
      {#each capability.actions || [] as action}
        <button
          type="button"
          class="btn-action btn-{action.variant || 'primary'}"
          on:click={() => runAction(action)}
          disabled={actionsInProgress[action.method]}
          title={action.label}
        >
          {actionsInProgress[action.method] ? '…' : action.label}
        </button>
      {/each}
    </div>
  </div>
</div>

<style>
  .power-card {
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    overflow: hidden;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.06);
    transition: border-color 0.2s ease, box-shadow 0.2s ease;
  }
  .power-card:hover {
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08);
  }
  .power-card-header {
    padding: 14px 18px;
    border-bottom: 1px solid var(--border-color);
  }
  .power-card-title {
    margin: 0;
    font-size: 15px;
    font-weight: 600;
    color: var(--text-primary);
    letter-spacing: -0.01em;
  }
  .power-card-desc {
    margin: 6px 0 0 0;
    font-size: 13px;
    line-height: 1.4;
    color: var(--text-secondary);
  }
  .power-card-body {
    padding: 18px;
    display: flex;
    flex-direction: column;
    gap: 18px;
  }
  .state-section {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .state-label {
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-secondary);
  }
  .state-value-row {
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .state-badge {
    padding: 6px 12px;
    border-radius: 6px;
    font-size: 13px;
    font-weight: 600;
    display: inline-block;
  }
  .state-badge.state-on {
    background: var(--success-bg);
    color: var(--success-text);
  }
  .state-badge.state-off {
    background: var(--danger-bg);
    color: var(--danger-text);
  }
  .state-badge.state-unknown {
    background: var(--bg-tertiary);
    color: var(--text-secondary);
  }
  .btn-refresh {
    padding: 6px;
    background: var(--bg-secondary);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    cursor: pointer;
    color: var(--text-secondary);
    transition: background 0.2s, color 0.2s, border-color 0.2s;
  }
  .btn-refresh:hover:not(:disabled) {
    background: var(--bg-tertiary);
    color: var(--text-primary);
    border-color: var(--border-color);
  }
  .btn-refresh:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }
  .action-section {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
  }
  .btn-action {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 10px 16px;
    min-width: 0;
    flex: 1;
    border-radius: 6px;
    font-weight: 500;
    font-size: 13px;
    cursor: pointer;
    border: 1px solid transparent;
    transition: opacity 0.2s, background 0.2s, border-color 0.2s;
  }
  .btn-action:hover:not(:disabled) {
    opacity: 0.95;
  }
  .btn-action:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }
  .btn-action.btn-success {
    background: var(--success-color);
    color: white;
    border-color: var(--success-color);
  }
  .btn-action.btn-success:hover:not(:disabled) {
    background: var(--success-text);
    border-color: var(--success-text);
  }
  .btn-action.btn-danger {
    background: var(--danger-color);
    color: white;
    border-color: var(--danger-color);
  }
  .btn-action.btn-danger:hover:not(:disabled) {
    filter: brightness(1.05);
  }
  .btn-action.btn-warning {
    background: var(--warning-color);
    color: white;
    border-color: var(--warning-color);
  }
  .btn-action.btn-warning:hover:not(:disabled) {
    filter: brightness(1.05);
  }
  .btn-action.btn-primary {
    background: var(--accent-color);
    color: white;
    border-color: var(--accent-color);
  }
  .btn-action.btn-primary:hover:not(:disabled) {
    background: var(--accent-dark);
    border-color: var(--accent-dark);
  }
</style>
