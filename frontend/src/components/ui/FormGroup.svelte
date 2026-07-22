<script>
  export let label = '';
  export let forId = '';
  export let help = '';
  export let required = false;
</script>

<div class="form-group">
  {#if label || $$slots.label}
    <label for={forId || undefined}>
      {#if $$slots.label}
        <slot name="label" />
      {:else}
        {label}
      {/if}
      {#if required}
        <span class="required">*</span>
      {/if}
    </label>
  {/if}
  <div class="form-control">
    <slot />
  </div>
  {#if help}
    <span class="field-help">{help}</span>
  {/if}
  <slot name="help" />
</div>

<style>
  .form-group {
    margin-bottom: 16px;
  }

  label {
    display: block;
    margin-bottom: 8px;
    font-weight: 600;
    color: var(--text-primary);
  }

  .required {
    color: var(--danger-color);
  }

  .field-help {
    display: block;
    margin-top: 4px;
    font-size: 12px;
    color: var(--text-secondary);
  }

  :global(.form-control input),
  :global(.form-control textarea),
  :global(.form-control select) {
    width: 100%;
    padding: 10px 12px;
    border: 2px solid var(--border-color);
    border-radius: 8px;
    font-size: 14px;
    font-family: inherit;
    background-color: var(--bg-primary);
    color: var(--text-primary);
    transition: border-color 0.2s ease, box-shadow 0.2s ease;
  }

  :global(.form-control select) {
    padding-right: 36px;
    appearance: none;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%23475569' d='M6 8L1 3h10z'/%3E%3C/svg%3E");
    background-repeat: no-repeat;
    background-position: right 12px center;
    background-size: 12px;
    cursor: pointer;
  }

  :global([data-theme="dark"] .form-control select) {
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%23cbd5e1' d='M6 8L1 3h10z'/%3E%3C/svg%3E");
  }

  :global(.form-control input:focus),
  :global(.form-control textarea:focus),
  :global(.form-control select:focus) {
    outline: none;
    border-color: var(--accent-color);
    box-shadow: var(--focus-ring-accent);
  }

  :global(.form-control input::placeholder),
  :global(.form-control textarea::placeholder) {
    color: var(--text-tertiary);
  }

  :global(.form-control input[type="checkbox"]) {
    width: auto;
  }
</style>
