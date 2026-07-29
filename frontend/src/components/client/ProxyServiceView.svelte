<script>
  import ProxyCredentialsPanel from './ProxyCredentialsPanel.svelte';

  /** http_proxy service detail payload. */
  export let service;
</script>

{#if service.proxy_credentials_available}
  <ProxyCredentialsPanel serviceId={service.id} canRotate={service.proxy_rotate_available} />
{:else}
  <p class="muted">
    Proxy credentials are not available for this service. Contact support if you believe this is a mistake.
  </p>
{/if}

<style>
  .muted {
    color: var(--text-tertiary);
    font-size: 14px;
  }
</style>
