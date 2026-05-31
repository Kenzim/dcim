<script>
  import { onMount, onDestroy } from 'svelte';
  import Button from './Button.svelte';

  export let title = '';
  export let size = 'default'; // 'default' | 'large'
  export let onClose;

  function handleKeydown(e) {
    if (e.key === 'Escape') onClose?.();
  }

  onMount(() => {
    document.addEventListener('keydown', handleKeydown);
  });
  onDestroy(() => {
    document.removeEventListener('keydown', handleKeydown);
  });
</script>

<div
  class="modal-overlay"
  role="presentation"
  tabindex="-1"
  on:click={(e) => e.target === e.currentTarget && onClose?.()}
>
  <!-- stopPropagation only (clicks inside must not close overlay) -->
  <!-- svelte-ignore a11y-no-static-element-interactions -->
  <!-- svelte-ignore a11y-no-noninteractive-element-interactions -->
  <div
    class="modal-content"
    class:modal-large={size === 'large'}
    role="dialog"
    aria-modal="true"
    aria-labelledby="modal-title"
    on:click|stopPropagation
    on:keydown|stopPropagation
  >
    <div class="modal-header">
      <h3 id="modal-title">{title}</h3>
      <Button iconOnly on:click={onClose} ariaLabel="Close">
        <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
        </svg>
      </Button>
    </div>
    <div class="modal-body">
      <slot />
    </div>
    {#if $$slots.footer}
      <div class="modal-footer">
        <slot name="footer" />
      </div>
    {/if}
  </div>
</div>

<style>
  .modal-overlay {
    position: fixed;
    inset: 0;
    background: var(--overlay-bg);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
    padding: 20px;
  }

  .modal-content {
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: 12px;
    box-shadow: var(--shadow-xl);
    color: var(--text-primary);
    width: 100%;
    max-width: 500px;
    max-height: 90vh;
    overflow-y: auto;
    transition: background-color 0.3s ease, border-color 0.3s ease;
  }

  .modal-content.modal-large {
    max-width: 900px;
  }

  .modal-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 20px 24px;
    border-bottom: 1px solid var(--border-color);
    background: var(--bg-primary);
    position: sticky;
    top: 0;
    z-index: 10;
  }

  .modal-header h3 {
    margin: 0;
    font-size: 20px;
    font-weight: 600;
  }

  .modal-body {
    padding: 24px;
  }

  .modal-footer {
    display: flex;
    justify-content: flex-end;
    gap: 12px;
    padding: 20px 24px;
    border-top: 1px solid var(--border-color);
    background: var(--bg-primary);
    position: sticky;
    bottom: 0;
  }
</style>
