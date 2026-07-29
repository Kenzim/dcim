<script>
  // Small hover/focus popover used to surface extra detail on otherwise
  // very simple elements (a single number, a single status word) without
  // resorting to a modal. Works with mouse (hover) and keyboard (focus)
  // alike so the same detail is reachable both ways.
  export let position = 'bottom'; // 'bottom' | 'top'

  let visible = false;
  let hideTimeout = null;

  function show() {
    if (hideTimeout) {
      clearTimeout(hideTimeout);
      hideTimeout = null;
    }
    visible = true;
  }

  function hide() {
    hideTimeout = setTimeout(() => {
      visible = false;
      hideTimeout = null;
    }, 100);
  }
</script>

<div
  class="hover-tip"
  role="group"
  on:mouseenter={show}
  on:mouseleave={hide}
  on:focusin={show}
  on:focusout={hide}
>
  <slot />
  {#if visible}
    <div class="hover-tip-panel" class:position-top={position === 'top'} role="tooltip">
      <slot name="content" />
    </div>
  {/if}
</div>

<style>
  .hover-tip {
    position: relative;
    display: block;
  }

  .hover-tip-panel {
    position: absolute;
    top: calc(100% + 8px);
    left: 0;
    min-width: 200px;
    max-width: 300px;
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: 10px;
    box-shadow: var(--shadow-lg);
    padding: 12px 14px;
    font-size: 13px;
    line-height: 1.5;
    color: var(--text-primary);
    z-index: 50;
    white-space: normal;
    pointer-events: none;
  }

  .hover-tip-panel.position-top {
    top: auto;
    bottom: calc(100% + 8px);
  }
</style>
