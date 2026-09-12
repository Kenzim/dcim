<script>
  import { Alert, Button } from '../ui/index.js';
  import { createClientIpmiTicket, getClientVirtualMedia, insertClientVirtualMedia, ejectClientVirtualMedia } from '../../lib/api.js';

  /** Bare-metal service detail payload. */
  export let service;
  /** @type {'overview' | 'console' | 'media'} */
  export let activeTab = 'overview';

  let busy = false;
  let consoleError = '';
  let media = null;
  let mediaError = '';
  let loadingMedia = false;
  let selectedIso = '';
  let bootOnce = false;
  let mediaBusy = false;

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

  function openKvm() {
    if (!service?.id) return;
    window.open(
      `/api/client/services/${service.id}/kvm-popup`,
      `rackflow_kvm_${service.id}`,
      'width=1024,height=768,resizable=yes,scrollbars=yes'
    );
  }

  function openSol() {
    if (!service?.id) return;
    window.open(
      `/api/client/services/${service.id}/sol-popup`,
      `rackflow_sol_${service.id}`,
      'width=1024,height=768,resizable=yes,scrollbars=yes'
    );
  }

  let mediaLoadedKey = '';

  async function loadMedia() {
    if (!service?.id || !service.virtual_media_available) return;
    const key = `${service.id}:${activeTab}`;
    if (mediaLoadedKey === key && media) return;
    mediaLoadedKey = key;
    loadingMedia = true;
    mediaError = '';
    try {
      media = await getClientVirtualMedia(service.id);
    } catch (e) {
      mediaError = e.message || String(e);
      media = null;
    } finally {
      loadingMedia = false;
    }
  }

  $: if (activeTab === 'media' && service?.virtual_media_available) {
    loadMedia();
  }

  async function handleMount() {
    if (!selectedIso) return;
    mediaBusy = true;
    mediaError = '';
    try {
      media = await insertClientVirtualMedia(service.id, selectedIso, bootOnce);
    } catch (e) {
      mediaError = e.message || String(e);
    } finally {
      mediaBusy = false;
    }
  }

  async function handleEject() {
    mediaBusy = true;
    mediaError = '';
    try {
      media = await ejectClientVirtualMedia(service.id);
    } catch (e) {
      mediaError = e.message || String(e);
    } finally {
      mediaBusy = false;
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
      Console links are single-use and expire shortly. Open IPMI uses the BMC web UI proxy; Open KVM is a native HTML5 console popup; Open Serial is Serial-over-LAN.
    </p>
    {#if consoleError}
      <Alert type="error">{consoleError}</Alert>
    {/if}
    <div class="console-actions">
      {#if service.ipmi_available}
      <Button disabled={busy} on:click={openIpmi}>
        <svg slot="icon" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" width="15" height="15">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" />
        </svg>
        {busy ? 'Opening…' : 'Open IPMI console'}
      </Button>
      {/if}
      {#if service.kvm_console_available}
      <Button on:click={openKvm}>
        <svg slot="icon" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" width="15" height="15">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 10l4.553-2.276A1 1 0 0121 8.618v6.764a1 1 0 01-1.447.894L15 14M5 18h8a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v8a2 2 0 002 2z" />
        </svg>
        Open KVM
      </Button>
      {/if}
      {#if service.sol_console_available}
      <Button on:click={openSol}>
        <svg slot="icon" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" width="15" height="15">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
        </svg>
        Open Serial
      </Button>
      {/if}
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
{:else if activeTab === 'media'}
  <section class="stack">
    <p class="muted">
      Mount an ISO from the operator catalog as a virtual CD on this server’s BMC. Optional next-boot-from-CD does not reboot the machine.
    </p>
    {#if mediaError}
      <Alert type="error">{mediaError}</Alert>
    {/if}
    {#if loadingMedia}
      <p class="muted">Loading virtual media…</p>
    {:else}
      <p class="muted">
        {#if media?.inserted}
          Mounted: <code>{media.image_name || 'ISO'}</code>
        {:else}
          No virtual CD inserted
        {/if}
      </p>
      <div class="media-row">
        <select bind:value={selectedIso} disabled={mediaBusy}>
          <option value="">Select ISO</option>
          {#each (media?.isos || []) as iso}
            <option value={iso.filename}>{iso.filename}</option>
          {/each}
        </select>
        <label class="muted">
          <input type="checkbox" bind:checked={bootOnce} disabled={mediaBusy} />
          Set next boot to CD-ROM
        </label>
      </div>
      <div class="console-actions">
        <Button disabled={mediaBusy || !selectedIso} on:click={handleMount}>{mediaBusy ? 'Working…' : 'Mount'}</Button>
        <Button variant="secondary" disabled={mediaBusy || !media?.inserted} on:click={handleEject}>Eject</Button>
      </div>
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
  .console-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  .media-row {
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
    align-items: center;
  }
  .media-row select {
    min-width: 220px;
    padding: 8px 10px;
    border-radius: 8px;
    border: 1px solid var(--border-color);
    background: var(--bg-primary);
    color: var(--text-primary);
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
