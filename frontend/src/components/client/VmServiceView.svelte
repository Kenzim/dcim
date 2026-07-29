<script>
  import { createEventDispatcher } from 'svelte';
  import { Alert, Button } from '../ui/index.js';
  import ConsoleButtons from './ConsoleButtons.svelte';
  import BackupsPanel from './BackupsPanel.svelte';
  import SshKeysPanel from './SshKeysPanel.svelte';
  import StrategyActionsPanel from './StrategyActionsPanel.svelte';
  import ReinstallModal from './ReinstallModal.svelte';

  /** VM service detail payload. */
  export let service;
  /** @type {'overview' | 'console' | 'backups' | 'reinstall' | 'settings'} */
  export let activeTab = 'overview';

  const dispatch = createEventDispatcher();

  let reinstallOpen = false;
  let reinstallQueued = false;

  function onReinstallDone() {
    reinstallQueued = true;
    dispatch('refresh');
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
    <div class="fact">
      <dt>Placement</dt>
      <dd>
        {#if service.proxmox_vmid != null}
          cluster {service.proxmox_cluster_id ?? '—'} / {service.proxmox_node_name || '—'} / VMID {service.proxmox_vmid}
        {:else}
          Not yet placed
        {/if}
      </dd>
    </div>
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
    {#if service.guest_state}
      <div class="fact">
        <dt>Guest state</dt>
        <dd class="cap">{service.guest_state}</dd>
      </div>
    {/if}
    <div class="fact">
      <dt>SSH keys</dt>
      <dd>{service.has_ssh_public_keys ? 'Configured' : 'Not set'}</dd>
    </div>
  </dl>
{:else if activeTab === 'console'}
  <section class="stack">
    <p class="muted">
      The console opens in its own popup window. Links are single-use and expire shortly.
    </p>
    {#if service.power_state === 'off'}
      <Alert type="warning">The VM is powered off — power it on before opening a console.</Alert>
    {/if}
    <ConsoleButtons {service} />
  </section>
{:else if activeTab === 'backups'}
  <BackupsPanel {service} />
{:else if activeTab === 'reinstall'}
  <section class="stack">
    <header class="panel-head">
      <h3>Reinstall</h3>
      <p class="muted">
        Rebuilds the guest from its OS template at the same VMID. Disks are replaced; backups stay available to restore.
      </p>
    </header>
    {#if reinstallQueued}
      <Alert type="success">Reinstall queued — the VM is being rebuilt. This can take several minutes.</Alert>
    {/if}
    <div>
      <Button variant="danger" on:click={() => (reinstallOpen = true)}>Reinstall VM</Button>
    </div>
  </section>
{:else if activeTab === 'settings'}
  <div class="settings-stack">
    {#if service.accepts_ssh_key}
      <SshKeysPanel {service} on:saved={() => dispatch('refresh')} />
    {/if}
    <StrategyActionsPanel serviceId={service.id} />
  </div>
{/if}

{#if reinstallOpen}
  <ReinstallModal {service} onClose={() => (reinstallOpen = false)} on:done={onReinstallDone} />
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
  .panel-head h3 {
    margin: 0 0 4px;
    font-size: 16px;
    font-weight: 700;
  }
  .settings-stack {
    display: flex;
    flex-direction: column;
    gap: 28px;
  }
</style>
