<script>
  /** @type {{ value: string, label: string }[]} */
  export let options = [];
  /** Bound selected values (string ids). */
  export let value = [];
  export let label = '';
  export let emptyText = 'No options available';
  /** Approximate visible rows before scroll. */
  export let size = 5;
  export let disabled = false;

  $: selectedSet = new Set((value || []).map((v) => String(v)));
  $: listMaxHeight = `${Math.max(2, size) * 32}px`;

  function toggle(optionValue) {
    if (disabled) return;
    const key = String(optionValue);
    if (selectedSet.has(key)) {
      value = (value || []).filter((v) => String(v) !== key);
    } else {
      value = [...(value || []), key];
    }
  }
</script>

<div class="multi-select" class:disabled aria-disabled={disabled || undefined}>
  {#if label}
    <div class="multi-select-label">{label}</div>
  {/if}
  <div class="multi-select-list" style="max-height: {listMaxHeight}" role="group" aria-label={label || 'Multi select'}>
    {#if options.length === 0}
      <div class="multi-select-empty">{emptyText}</div>
    {:else}
      {#each options as opt}
        <label class="multi-select-option" class:selected={selectedSet.has(String(opt.value))}>
          <input
            type="checkbox"
            checked={selectedSet.has(String(opt.value))}
            {disabled}
            on:change={() => toggle(opt.value)}
          />
          <span>{opt.label}</span>
        </label>
      {/each}
    {/if}
  </div>
</div>

<style>
  .multi-select {
    display: flex;
    flex-direction: column;
    gap: 6px;
    min-width: 0;
  }

  .multi-select-label {
    font-size: 12px;
    font-weight: 600;
    color: var(--text-secondary);
  }

  .multi-select-list {
    display: flex;
    flex-direction: column;
    gap: 2px;
    overflow-y: auto;
    padding: 6px;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background: var(--bg-secondary);
    color: var(--text-primary);
  }

  .multi-select-option {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    padding: 6px 8px;
    border-radius: 4px;
    font-size: 13px;
    line-height: 1.35;
    color: var(--text-primary);
    cursor: pointer;
    user-select: none;
  }

  .multi-select-option:hover {
    background: color-mix(in srgb, var(--bg-tertiary) 70%, transparent);
  }

  .multi-select-option.selected {
    background: color-mix(in srgb, var(--accent-color) 18%, transparent);
  }

  .multi-select-option input {
    margin: 2px 0 0;
    width: auto;
    flex-shrink: 0;
    accent-color: var(--accent-color);
  }

  .multi-select-empty {
    padding: 10px 8px;
    font-size: 12px;
    color: var(--text-tertiary);
    font-style: italic;
  }

  .multi-select.disabled {
    opacity: 0.6;
    pointer-events: none;
  }
</style>
