<script>
  export let size = 'default'; // 'default' | 'small'
</script>

<span class="spinner" class:spinner-small={size === 'small'} role="status" aria-hidden="true"></span>

<style>
  .spinner {
    width: 40px;
    height: 40px;
    border: 3px solid var(--border-color);
    border-top-color: var(--accent-color);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
    display: inline-block;
  }

  .spinner-small {
    width: 20px;
    height: 20px;
    border-width: 2px;
  }

  @keyframes spin {
    to {
      transform: rotate(360deg);
    }
  }
</style>
