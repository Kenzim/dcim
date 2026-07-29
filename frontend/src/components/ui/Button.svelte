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

<!-- Styles live in app.css (.btn / .btn-*) so raw <button class="btn-primary"> matches. -->
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
  on:click
>
  {#if iconOnly}
    <slot />
  {:else}
    <slot name="icon" />
    <slot />
  {/if}
</button>
