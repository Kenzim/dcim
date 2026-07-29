<script>
  /** @type {'success' | 'danger' | 'warning' | 'info' | 'muted' | 'accent'} */
  export let variant = 'muted';
  /** Show a small status dot before the label. */
  export let dot = false;
  /** Uppercase micro-label styling (default) or normal case. */
  export let caps = true;
</script>

<span class="badge badge-{variant}" class:badge-caps={caps}>
  {#if dot}<span class="badge-dot" aria-hidden="true"></span>{/if}
  <slot />
</span>

<style>
  .badge {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 4px 10px;
    border-radius: var(--radius-pill, 999px);
    font-size: 12px;
    font-weight: 650;
    line-height: 1.4;
    white-space: nowrap;
  }
  .badge-caps {
    text-transform: uppercase;
    letter-spacing: 0.04em;
    font-size: 11px;
  }
  .badge-dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: currentColor;
    flex-shrink: 0;
  }
  .badge-success {
    background: var(--success-bg);
    color: var(--success-text);
  }
  .badge-danger {
    background: var(--danger-bg);
    color: var(--danger-text);
  }
  .badge-warning {
    background: var(--warning-bg);
    color: var(--warning-text);
  }
  .badge-info {
    background: var(--info-bg);
    color: var(--info-text);
  }
  .badge-muted {
    background: var(--bg-tertiary);
    color: var(--text-secondary);
  }
  .badge-accent {
    background: var(--portal-accent-soft, var(--info-bg));
    color: var(--portal-accent, var(--info-text));
  }
</style>
