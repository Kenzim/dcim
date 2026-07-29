<script>
  import { Alert, Button } from '../ui/index.js';
  import { createClientIpmiTicket } from '../../lib/api.js';

  /** Bare-metal service detail payload. */
  export let service;
  /** @type {'overview' | 'console'} */
  export let activeTab = 'overview';

  let busy = false;
  let consoleError = '';

  async function openIpmi() {
    if (busy) return;
    busy = true;
    consoleError = '';
    try {
      const ticket = await createClientIpmiTicket(service.id);
      window.open(ticket.launch_url, '_blank', 'noopener');
    } catch (e) {
      consoleError = e.message || String(e);
    } finally {
      busy = false;
    }
  }
</script>

{#if activeTab === 'overview'}
  <dl class="facts">
    <div class="fact">
      <dt>Status</dt>
      <dd class="cap">{service.status}</dd>
    </div>
    <div class="fact">
      <dt>Power</dt>
      <dd class="cap">{service.power_state}</dd>
    </div>
    {#if service.primary_ip}
      <div class="fact">
        <dt>Primary IP</dt>
        <dd><code>{service.primary_ip}</code></dd>
      </div>
    {/if}
    {#if service.server_name}
      <div class="fact">
        <dt>Server</dt>
        <dd>{service.server_name}</dd>
      </div>
    {/if}
    {#if service.os_code}
      <div class="fact">
        <dt>OS profile</dt>
        <dd>{service.os_code}</dd>
      </div>
    {/if}
    {#if service.product_code}
      <div class="fact">
        <dt>Product</dt>
        <dd>{service.product_code}</dd>
      </div>
    {/if}
    {#if service.server_enabled === false}
      <div class="fact">
        <dt>Administrative state</dt>
        <dd class="warn">Disabled by the operator</dd>
      </div>
    {/if}
  </dl>

  {#if service.installation}
    <div class="installation">
      <h3>Installation</h3>
      <div class="install-row">
        <span class="cap install-status install-{service.installation.status}">{service.installation.status}</span>
        {#if service.installation.os_name}
          <span class="muted">{service.installation.os_name}</span>
        {/if}
      </div>
      {#if service.installation.status === 'running' || service.installation.status === 'pending'}
        <div class="progress" role="progressbar" aria-valuenow={service.installation.progress_percent || 0} aria-valuemin="0" aria-valuemax="100">
          <div class="progress-fill" style={`width: ${service.installation.progress_percent || 0}%`}></div>
        </div>
        <span class="muted">{service.installation.progress_percent || 0}%</span>
      {/if}
      {#if service.installation.error_message}
        <p class="install-error">{service.installation.error_message}</p>
      {/if}
    </div>
  {/if}
{:else if activeTab === 'console'}
  <section class="stack">
    <p class="muted">
      Opens the IPMI/KVM console in a new tab. Console links are single-use and expire shortly.
    </p>
    {#if consoleError}
      <Alert type="error">{consoleError}</Alert>
    {/if}
    <div>
      <Button disabled={busy} on:click={openIpmi}>
        <svg slot="icon" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" width="15" height="15">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" />
        </svg>
        {busy ? 'Opening…' : 'Open IPMI console'}
      </Button>
    </div>
    {#if service.ipmi_viewer_username || service.ipmi_viewer_password}
      <dl class="facts creds">
        {#if service.ipmi_viewer_username}
          <div class="fact">
            <dt>Console username</dt>
            <dd><code>{service.ipmi_viewer_username}</code></dd>
          </div>
        {/if}
        {#if service.ipmi_viewer_password}
          <div class="fact">
            <dt>Console password</dt>
            <dd><code>{service.ipmi_viewer_password}</code></dd>
          </div>
        {/if}
      </dl>
    {/if}
  </section>
{/if}

<style>
  .facts {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    gap: 16px 24px;
    margin: 0;
  }
  .fact dt {
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    color: var(--text-tertiary);
    margin-bottom: 4px;
  }
  .fact dd {
    margin: 0;
    font-size: 14px;
    font-weight: 600;
    color: var(--text-primary);
    word-break: break-word;
  }
  .fact dd code {
    font-family: var(--font-mono);
    font-size: 13px;
  }
  .cap {
    text-transform: capitalize;
  }
  .warn {
    color: var(--warning-text);
  }
  .installation {
    margin-top: 22px;
    padding-top: 18px;
    border-top: 1px solid var(--border-color);
  }
  .installation h3 {
    margin: 0 0 10px;
    font-size: 15px;
    font-weight: 700;
  }
  .install-row {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 8px;
  }
  .install-status {
    font-weight: 700;
    font-size: 13px;
  }
  .install-completed {
    color: var(--success-color);
  }
  .install-failed {
    color: var(--danger-color);
  }
  .install-running,
  .install-pending {
    color: var(--info-color);
  }
  .progress {
    height: 8px;
    border-radius: var(--radius-pill);
    background: var(--bg-tertiary);
    overflow: hidden;
    margin-bottom: 6px;
  }
  .progress-fill {
    height: 100%;
    border-radius: var(--radius-pill);
    background: var(--portal-accent);
    transition: width 0.4s ease;
  }
  .install-error {
    margin: 8px 0 0;
    font-size: 13px;
    color: var(--danger-text);
  }
  .stack {
    display: flex;
    flex-direction: column;
    gap: 14px;
    align-items: flex-start;
  }
  .muted {
    margin: 0;
    color: var(--text-tertiary);
    font-size: 13px;
  }
  .creds {
    margin-top: 4px;
    padding: 14px 16px;
    border: 1px solid var(--border-color);
    border-radius: var(--radius-md);
    background: var(--bg-secondary);
    width: 100%;
    max-width: 460px;
  }
</style>
