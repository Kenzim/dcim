<script>
  /** @type {'error' | 'danger' | 'success' | 'warning' | 'info'} */
  export let type = 'info';
</script>

<div class="alert" class:alert-error={type === 'error'} class:alert-danger={type === 'danger'} class:alert-success={type === 'success'} class:alert-warning={type === 'warning'} class:alert-info={type === 'info'} role="alert">
  {#if $$slots.icon}
    <span class="alert-icon">
      <slot name="icon" />
    </span>
  {:else if type === 'error' || type === 'danger'}
    <svg xmlns="http://www.w3.org/2000/svg" class="alert-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
    </svg>
  {:else if type === 'success'}
    <svg xmlns="http://www.w3.org/2000/svg" class="alert-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
    </svg>
  {/if}
  <slot />
</div>

<style>
  .alert {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 12px 16px;
    border-radius: 10px;
    margin-bottom: 16px;
  }

  .alert-icon {
    width: 20px;
    height: 20px;
    flex-shrink: 0;
  }

  .alert-error,
  .alert-danger {
    background: var(--danger-bg);
    color: var(--danger-text);
    border: 1px solid var(--danger-color);
  }

  .alert-success {
    background: var(--success-bg);
    color: var(--success-text);
    border: 1px solid var(--success-color);
  }

  .alert-warning {
    background: var(--warning-bg);
    color: var(--warning-text);
    border: 1px solid var(--warning-color);
  }

  .alert-info {
    background: var(--info-bg);
    color: var(--info-text);
    border: 1px solid var(--info-color);
  }
</style>
