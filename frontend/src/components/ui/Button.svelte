<script>
  /** @type {'button' | 'submit' | 'reset'} */
  export let type = 'button';
  /** @type {'primary' | 'secondary' | 'danger'} */
  export let variant = 'primary';
  /** @type {'default' | 'small'} */
  export let size = 'default';
  export let iconOnly = false;
  export let disabled = false;
  export let ariaLabel = undefined;
</script>

<button
  {type}
  {disabled}
  class="btn"
  class:btn-primary={variant === 'primary' && !iconOnly}
  class:btn-secondary={variant === 'secondary' && !iconOnly}
  class:btn-danger={variant === 'danger' && !iconOnly}
  class:btn-icon-only={iconOnly}
  class:btn-icon-only-danger={iconOnly && variant === 'danger'}
  class:btn-small={size === 'small'}
  aria-label={ariaLabel}
  {...$$restProps}
>
  {#if iconOnly}
    <slot />
  {:else}
    <slot name="icon" />
    <slot />
  {/if}
</button>

<style>
  .btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    font-family: inherit;
    cursor: pointer;
    transition: all 0.2s ease;
    border: none;
  }

  .btn-primary {
    padding: 10px 20px;
    background: var(--accent-color);
    color: white;
    border-radius: 8px;
    font-weight: 600;
    font-size: 14px;
  }
  .btn-primary:hover:not(:disabled) {
    background: var(--accent-dark);
    transform: translateY(-2px);
    box-shadow: var(--shadow-lg);
  }

  .btn-secondary {
    padding: 10px 20px;
    background: var(--bg-tertiary);
    color: var(--text-primary);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    font-weight: 600;
    font-size: 14px;
  }
  .btn-secondary:hover:not(:disabled) {
    background: var(--accent-color);
    color: white;
    border-color: var(--accent-color);
  }

  .btn-danger {
    padding: 10px 20px;
    background: var(--danger-color);
    color: white;
    border: 1px solid var(--danger-color);
    border-radius: 8px;
    font-weight: 600;
    font-size: 14px;
  }
  .btn-danger:hover:not(:disabled) {
    filter: brightness(1.1);
  }

  .btn-icon-only {
    padding: 6px;
    background: var(--bg-tertiary);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    color: var(--text-primary);
  }
  .btn-icon-only:hover {
    background: var(--bg-secondary);
    border-color: var(--accent-color);
    color: var(--accent-color);
    transform: translateY(-1px);
  }
  .btn-icon-only.btn-icon-only-danger {
    border-color: var(--danger-color);
    color: var(--danger-color);
  }
  .btn-icon-only.btn-icon-only-danger:hover {
    background: var(--danger-color);
    color: white;
    border-color: var(--danger-color);
  }
  .btn-icon-only :global(svg) {
    width: 18px;
    height: 18px;
  }

  .btn-small {
    padding: 6px 12px;
    font-size: 12px;
  }
  .btn-icon-only.btn-small :global(svg) {
    width: 14px;
    height: 14px;
  }
  .btn-icon-only.btn-small {
    padding: 4px;
  }

  .btn:disabled {
    opacity: 0.6;
    cursor: not-allowed;
    transform: none;
  }
</style>
