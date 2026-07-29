<script>
  export let label = '';
  /** @type {string | number} */
  export let value = '';
  export let hint = '';
  /** @type {'default' | 'accent' | 'success' | 'warning' | 'danger'} */
  export let tone = 'default';
</script>

<div class="tile tile-{tone}">
  <span class="tile-label">{label}</span>
  <span class="tile-value">{value}</span>
  {#if hint}<span class="tile-hint">{hint}</span>{/if}
</div>

<style>
  .tile {
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding: 16px 18px;
    background: var(--portal-card-bg, var(--bg-primary));
    border: 1px solid var(--portal-card-border, var(--border-color));
    border-radius: var(--radius-lg, 14px);
    box-shadow: var(--portal-card-shadow, var(--shadow-sm));
    min-width: 0;
  }
  .tile-label {
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    color: var(--text-tertiary);
  }
  .tile-value {
    font-size: 26px;
    font-weight: 750;
    letter-spacing: -0.02em;
    line-height: 1.15;
    color: var(--text-primary);
  }
  .tile-hint {
    font-size: 12px;
    color: var(--text-secondary);
  }
  .tile-accent .tile-value {
    color: var(--portal-accent, var(--accent-color));
  }
  .tile-success .tile-value {
    color: var(--success-color);
  }
  .tile-warning .tile-value {
    color: var(--warning-color);
  }
  .tile-danger .tile-value {
    color: var(--danger-color);
  }
</style>
