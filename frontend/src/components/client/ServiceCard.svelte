<script>
  import ServiceStatusBadge from './ServiceStatusBadge.svelte';
  import PowerControls from './PowerControls.svelte';

  /** Service from the list payload. */
  export let service;
  /** Show compact quick-power buttons in the footer (dashboard). */
  export let showPower = false;

  const TYPE_LABELS = {
    vm: 'Virtual machine',
    bare_metal: 'Bare metal',
    http_proxy: 'HTTP proxy',
  };

  $: typeLabel = TYPE_LABELS[service.service_type] || 'Service';

  let copied = false;
  let copyTimer = null;

  async function copyIp() {
    if (!service.primary_ip) return;
    try {
      await navigator.clipboard.writeText(service.primary_ip);
      copied = true;
      clearTimeout(copyTimer);
      copyTimer = setTimeout(() => (copied = false), 1500);
    } catch (_) {
      // Clipboard unavailable (non-secure context) — ignore.
    }
  }
</script>

<article class="card">
  <header class="card-head">
    <span class="type-tile type-{service.service_type}" aria-hidden="true">
      {#if service.service_type === 'vm'}
        <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" width="20" height="20">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M9 3v2m6-2v2M9 19v2m6-2v2M5 9H3m2 6H3m18-6h-2m2 6h-2M7 19h10a2 2 0 002-2V7a2 2 0 00-2-2H7a2 2 0 00-2 2v10a2 2 0 002 2zM9 9h6v6H9V9z" />
        </svg>
      {:else if service.service_type === 'http_proxy'}
        <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" width="20" height="20">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M21 12a9 9 0 01-9 9m9-9a9 9 0 00-9-9m9 9H3m9 9a9 9 0 01-9-9m9 9c1.657 0 3-4.03 3-9s-1.343-9-3-9m0 18c-1.657 0-3-4.03-3-9s1.343-9 3-9m-9 9a9 9 0 019-9" />
        </svg>
      {:else}
        <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" width="20" height="20">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M5 12h14M5 12a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v4a2 2 0 01-2 2M5 12a2 2 0 00-2 2v4a2 2 0 002 2h14a2 2 0 002-2v-4a2 2 0 00-2-2m-2-4h.01M17 16h.01" />
        </svg>
      {/if}
    </span>
    <div class="card-title">
      <h3>{service.name}</h3>
      <span class="type-label">{typeLabel}</span>
    </div>
    <ServiceStatusBadge status={service.status} powerState={service.power_state} />
  </header>

  <div class="card-body">
    {#if service.primary_ip}
      <div class="fact">
        <span class="fact-label">Primary IP</span>
        <span class="fact-value">
          <code>{service.primary_ip}</code>
          <button type="button" class="copy" on:click={copyIp} title="Copy IP" aria-label="Copy IP address">
            {#if copied}
              <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" width="13" height="13">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.4" d="M5 13l4 4L19 7" />
              </svg>
            {:else}
              <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" width="13" height="13">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
              </svg>
            {/if}
          </button>
        </span>
      </div>
    {/if}
    {#if service.product_code}
      <div class="fact">
        <span class="fact-label">Product</span>
        <span class="fact-value">{service.product_code}</span>
      </div>
    {/if}
    {#if service.service_type === 'vm' && service.proxmox_vmid != null}
      <div class="fact">
        <span class="fact-label">VMID</span>
        <span class="fact-value">{service.proxmox_vmid}</span>
      </div>
    {/if}
  </div>

  <footer class="card-foot">
    <a class="manage-link" href={`/client/services/${service.id}`}>
      Manage
      <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" width="14" height="14" aria-hidden="true">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
      </svg>
    </a>
    {#if showPower}
      <PowerControls {service} compact on:changed />
    {/if}
  </footer>
</article>

<style>
  .card {
    display: flex;
    flex-direction: column;
    background: var(--portal-card-bg);
    border: 1px solid var(--portal-card-border);
    border-radius: var(--radius-lg);
    box-shadow: var(--portal-card-shadow);
    padding: 18px;
    transition: box-shadow 0.2s ease, transform 0.2s ease, border-color 0.2s ease;
  }
  .card:hover {
    box-shadow: var(--portal-card-shadow-hover);
    transform: translateY(-2px);
    border-color: var(--portal-accent);
  }
  .card-head {
    display: flex;
    align-items: flex-start;
    gap: 12px;
  }
  .type-tile {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 40px;
    height: 40px;
    border-radius: var(--radius-md);
    flex-shrink: 0;
    background: var(--portal-accent-soft);
    color: var(--portal-accent);
  }
  .type-tile.type-bare_metal {
    background: var(--info-bg);
    color: var(--info-text);
  }
  .type-tile.type-http_proxy {
    background: var(--warning-bg);
    color: var(--warning-text);
  }
  .card-title {
    flex: 1;
    min-width: 0;
  }
  .card-title h3 {
    margin: 0;
    font-size: 15px;
    font-weight: 700;
    letter-spacing: -0.01em;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .type-label {
    font-size: 12px;
    color: var(--text-tertiary);
  }
  .card-body {
    display: flex;
    flex-wrap: wrap;
    gap: 8px 20px;
    margin: 14px 0;
  }
  .fact {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }
  .fact-label {
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    color: var(--text-tertiary);
  }
  .fact-value {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: 13px;
    font-weight: 600;
    color: var(--text-primary);
  }
  .fact-value code {
    font-family: var(--font-mono);
    font-size: 13px;
  }
  .copy {
    appearance: none;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 3px;
    background: none;
    border: 1px solid var(--border-color);
    border-radius: 5px;
    color: var(--text-tertiary);
    cursor: pointer;
  }
  .copy:hover {
    color: var(--portal-accent);
    border-color: var(--portal-accent);
  }
  .card-foot {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 10px;
    margin-top: auto;
    padding-top: 14px;
    border-top: 1px solid var(--border-color);
  }
  .manage-link {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    font-size: 13px;
    font-weight: 700;
    color: var(--portal-accent);
    text-decoration: none;
  }
  .manage-link:hover {
    text-decoration: underline;
  }
</style>
