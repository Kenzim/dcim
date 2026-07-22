<script>
  import { onMount } from 'svelte';
  import PageHeader from './PageHeader.svelte';
  import { navigate } from '../lib/router.js';
  import {
    getVmService,
    provisionVmService,
    vmPowerAction,
    destroyVmGuest,
    recreateVmGuest,
    deleteServiceCompletely,
    updateAdminServiceStatus,
  } from '../lib/api.js';

  export let serviceId;

  let service = null;
  let loading = true;
  let error = null;
  let busy = false;
  let statusDraft = 'pending';

  $: guestState = service?.vm_guest_state || 'unprovisioned';
  $: prov = service?.config?.vm_provision || {};
  $: guestTone = guestToneFor(guestState);
  $: serviceTone = serviceToneFor(service?.status);

  function guestToneFor(state) {
    switch (state) {
      case 'running': return 'ok';
      case 'stopped': return 'muted';
      case 'provisioning': return 'warn';
      case 'error': return 'bad';
      case 'destroyed': return 'bad';
      default: return 'muted';
    }
  }

  function serviceToneFor(status) {
    switch (status) {
      case 'active': return 'ok';
      case 'pending': return 'warn';
      case 'suspended': return 'warn';
      case 'terminated': return 'bad';
      default: return 'muted';
    }
  }

  async function load() {
    loading = true;
    error = null;
    try {
      service = await getVmService(serviceId);
      statusDraft = service?.status || 'pending';
    } catch (e) {
      error = e.message || String(e);
    } finally {
      loading = false;
    }
  }

  async function act(fn) {
    if (!service?.id) return;
    busy = true;
    error = null;
    try {
      service = await fn(service.id);
      service = await getVmService(service.id);
      statusDraft = service?.status || statusDraft;
    } catch (e) {
      error = e.message || String(e);
      try {
        service = await getVmService(serviceId);
      } catch (_) {
        /* keep prior service snapshot */
      }
    } finally {
      busy = false;
    }
  }

  async function deleteService() {
    if (!service?.id) return;
    if (!confirm(`Delete ${service.name} completely? This is permanent.`)) return;
    busy = true;
    error = null;
    try {
      await deleteServiceCompletely(service.id);
      navigate('/admin/vm-services');
    } catch (e) {
      error = e.message || String(e);
    } finally {
      busy = false;
    }
  }

  async function saveStatus() {
    if (!service?.id) return;
    busy = true;
    error = null;
    try {
      service = await updateAdminServiceStatus(service.id, statusDraft);
      statusDraft = service.status;
    } catch (e) {
      error = e.message || String(e);
    } finally {
      busy = false;
    }
  }

  async function terminateService() {
    if (!service?.id) return;
    if (!confirm(`Set ${service.name} to terminated?`)) return;
    statusDraft = 'terminated';
    await saveStatus();
  }

  async function destroyVm() {
    if (!service?.id) return;
    if (!confirm('Stop and destroy VM guest? Service and VMID reservation stay attached to this service.')) return;
    await act((id) => destroyVmGuest(id));
  }

  onMount(load);
</script>

<div class="page">
  <PageHeader title="VM Service Detail" />

  <div class="body">
    <div class="toolbar">
      <button class="btn-secondary" on:click={() => navigate('/admin/vm-services')}>← Back to VM Services</button>
      {#if busy}<span class="busy-label">Working…</span>{/if}
    </div>

    {#if loading}
      <p class="muted fill-msg">Loading…</p>
    {:else if error && !service}
      <p class="error fill-msg">{error}</p>
    {:else if service}
      {#if error}
        <div class="banner bad span-all">{error}</div>
      {/if}

      <header class="hero span-all">
        <div class="hero-main">
          <p class="eyebrow">VM service #{service.id}</p>
          <h2 class="title">{service.name}</h2>
          <p class="meta">
            {service.provisioning_source || 'billing'}
            · {service.owner_username || 'Unassigned owner'}
          </p>
        </div>
        <div class="hero-badges">
          <div class="status-pill tone-{guestTone}">
            <span class="pill-label">Guest</span>
            <span class="pill-value">{guestState}</span>
            <span class="pill-hint">live from Proxmox</span>
          </div>
          <div class="status-pill tone-{serviceTone}">
            <span class="pill-label">Service</span>
            <span class="pill-value">{service.status}</span>
          </div>
        </div>
      </header>

      <div class="col overview">
        <div class="fact-grid">
          <section class="fact">
            <h3>Placement</h3>
            <dl>
              <div><dt>Cluster</dt><dd>{service.proxmox_cluster_id ?? '—'}</dd></div>
              <div><dt>Node</dt><dd>{service.proxmox_node_name || '—'}</dd></div>
              <div><dt>VMID</dt><dd class="mono">{service.proxmox_vmid ?? '—'}</dd></div>
            </dl>
          </section>
          <section class="fact">
            <h3>Network</h3>
            <dl>
              <div><dt>Assigned IP</dt><dd class="mono">{service.vm_ip_address || '—'}</dd></div>
              <div><dt>Pool row</dt><dd class="mono">{service.vm_ip_allocation_id ?? '—'}</dd></div>
              <div><dt>Strategy</dt><dd>{service.vm_strategy_name || '—'}</dd></div>
            </dl>
          </section>
          <section class="fact">
            <h3>Provisioning</h3>
            <dl>
              <div><dt>Last run</dt><dd>{prov.status || '—'}</dd></div>
              <div><dt>Step</dt><dd>{prov.step || '—'}</dd></div>
              <div><dt>Template</dt><dd class="mono">{service.vm_template_id ?? '—'}</dd></div>
            </dl>
          </section>
        </div>

        {#if service.vm_guest_last_error}
          <div class="banner bad">
            <strong>Last guest error</strong>
            <span>{service.vm_guest_last_error}</span>
          </div>
        {/if}

        <section class="panel grow">
          <div class="panel-head">
            <h3>Service record</h3>
            <p>Billing/admin lifecycle for this service row (independent of guest power).</p>
          </div>
          <div class="status-editor">
            <label>
              Status
              <select bind:value={statusDraft} disabled={busy}>
                <option value="pending">pending</option>
                <option value="active">active</option>
                <option value="suspended">suspended</option>
                <option value="terminated">terminated</option>
              </select>
            </label>
            <button class="btn-secondary" disabled={busy || statusDraft === service.status} on:click={saveStatus}>
              Save Status
            </button>
            <button class="btn-danger" disabled={busy} on:click={terminateService}>Terminate Service</button>
          </div>
        </section>
      </div>

      <div class="col controls">
        <section class="panel">
          <div class="panel-head">
            <h3>Power</h3>
            <p>Controls the Proxmox guest. Guest state refreshes from the hypervisor on load and after each action.</p>
          </div>
          <div class="actions">
            <button class="btn-primary" disabled={busy} on:click={() => act((id) => vmPowerAction(id, 'on'))}>Power On</button>
            <button class="btn-secondary" disabled={busy} on:click={() => act((id) => vmPowerAction(id, 'off'))}>Power Off</button>
            <button class="btn-secondary" disabled={busy} on:click={() => act((id) => vmPowerAction(id, 'reboot'))}>Reboot</button>
          </div>
        </section>

        <section class="panel grow">
          <div class="panel-head">
            <h3>Lifecycle</h3>
            <p>Provision or recreate from the catalog template. Destroy removes the Proxmox VM but keeps this service record.</p>
          </div>
          <div class="actions">
            <button class="btn-primary" disabled={busy} on:click={() => act((id) => provisionVmService(id))}>Provision VM</button>
            <button class="btn-secondary" disabled={busy} on:click={() => act((id) => recreateVmGuest(id))}>Recreate VM</button>
            <button class="btn-secondary" disabled={busy} on:click={destroyVm}>Stop + Destroy VM</button>
          </div>
        </section>

        <section class="panel danger-zone">
          <div class="panel-head">
            <h3>Danger zone</h3>
            <p>Permanently delete the service and related records.</p>
          </div>
          <button class="btn-danger" disabled={busy} on:click={deleteService}>Delete Service Completely</button>
        </section>
      </div>
    {/if}
  </div>
</div>

<style>
  /* Fill the admin main pane (flex column under 100vh) — use full width, no tall scroll stack. */
  .page {
    flex: 1 1 auto;
    min-height: 0;
    height: 100%;
    max-height: 100%;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  .body {
    flex: 1;
    min-height: 0;
    display: grid;
    grid-template-columns: minmax(0, 1.15fr) minmax(320px, 0.85fr);
    grid-template-rows: auto auto 1fr;
    gap: 14px 16px;
    padding: 16px 24px 20px;
    overflow: hidden;
    align-content: stretch;
  }

  .toolbar {
    grid-column: 1 / -1;
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .busy-label {
    font-size: 13px;
    font-weight: 600;
    color: var(--text-tertiary);
  }
  .span-all { grid-column: 1 / -1; }
  .fill-msg { grid-column: 1 / -1; }

  .col {
    min-height: 0;
    display: flex;
    flex-direction: column;
    gap: 12px;
    overflow: auto;
  }
  .overview { grid-column: 1; grid-row: 3; }
  .controls { grid-column: 2; grid-row: 3; }

  .hero {
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    align-items: center;
    gap: 16px 24px;
    padding: 18px 22px;
    border: 1px solid var(--border-color);
    border-radius: 12px;
    background:
      linear-gradient(135deg, color-mix(in srgb, var(--accent-color) 12%, transparent), transparent 55%),
      var(--bg-primary);
  }
  .eyebrow {
    margin: 0 0 4px;
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: var(--text-tertiary);
  }
  .title {
    margin: 0;
    font-size: clamp(1.4rem, 2vw, 1.85rem);
    font-weight: 700;
    color: var(--text-primary);
    line-height: 1.2;
  }
  .meta {
    margin: 6px 0 0;
    color: var(--text-secondary);
    font-size: 14px;
  }
  .hero-badges {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
  }
  .status-pill {
    min-width: 148px;
    padding: 10px 14px;
    border-radius: 10px;
    border: 1px solid var(--border-color);
    background: var(--bg-secondary);
    display: grid;
    gap: 2px;
  }
  .pill-label {
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-tertiary);
  }
  .pill-value {
    font-size: 1.1rem;
    font-weight: 700;
    text-transform: lowercase;
  }
  .pill-hint {
    font-size: 11px;
    color: var(--text-tertiary);
  }
  .tone-ok .pill-value { color: var(--success-color); }
  .tone-warn .pill-value { color: var(--warning-color); }
  .tone-bad .pill-value { color: var(--danger-color); }
  .tone-muted .pill-value { color: var(--text-secondary); }

  .fact-grid {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 12px;
  }
  .fact {
    border: 1px solid var(--border-color);
    border-radius: 12px;
    padding: 14px 16px;
    background: var(--bg-primary);
  }
  .fact h3 {
    margin: 0 0 10px;
    font-size: 12px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-tertiary);
  }
  .fact dl {
    margin: 0;
    display: grid;
    gap: 8px;
  }
  .fact dl > div { display: grid; gap: 2px; }
  .fact dt {
    font-size: 12px;
    color: var(--text-tertiary);
    font-weight: 600;
  }
  .fact dd {
    margin: 0;
    font-size: 15px;
    font-weight: 600;
    color: var(--text-primary);
    word-break: break-word;
  }
  .mono {
    font-family: var(--font-mono);
    font-size: 13px;
    font-weight: 500;
  }

  .banner {
    display: grid;
    gap: 4px;
    padding: 12px 14px;
    border-radius: 10px;
    border: 1px solid var(--border-color);
    font-size: 14px;
  }
  .banner.bad {
    background: var(--danger-bg);
    color: var(--danger-text);
    border-color: color-mix(in srgb, var(--danger-color) 35%, var(--border-color));
  }

  .panel {
    border: 1px solid var(--border-color);
    border-radius: 12px;
    padding: 16px 18px;
    background: var(--bg-primary);
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .panel.grow { flex: 1; }
  .panel-head h3 {
    margin: 0 0 4px;
    font-size: 1rem;
  }
  .panel-head p {
    margin: 0;
    font-size: 13px;
    color: var(--text-secondary);
    max-width: 52ch;
  }
  .danger-zone {
    border-color: color-mix(in srgb, var(--danger-color) 40%, var(--border-color));
  }

  .actions { display: flex; flex-wrap: wrap; gap: 8px; }
  .status-editor { display: flex; flex-wrap: wrap; gap: 8px; align-items: end; }
  .status-editor label {
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-size: 13px;
    color: var(--text-secondary);
    font-weight: 600;
  }
  .status-editor select {
    min-width: 180px;
    padding: 8px 32px 8px 10px;
    border-radius: 8px;
    border: 1px solid var(--border-color);
    background-color: var(--bg-secondary);
    color: var(--text-primary);
    appearance: none;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%23475569' d='M6 8L1 3h10z'/%3E%3C/svg%3E");
    background-repeat: no-repeat;
    background-position: right 10px center;
    background-size: 12px;
    cursor: pointer;
  }
  :global([data-theme="dark"]) .status-editor select {
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%23cbd5e1' d='M6 8L1 3h10z'/%3E%3C/svg%3E");
  }

  .muted { color: var(--text-secondary); }
  .error { color: var(--danger-color); }
  .btn-primary, .btn-secondary, .btn-danger {
    padding: 8px 12px;
    border-radius: 8px;
    cursor: pointer;
    font-weight: 600;
  }
  .btn-primary { border: 0; background: var(--accent-color); color: white; }
  .btn-secondary { border: 1px solid var(--border-color); background: var(--bg-tertiary); color: var(--text-primary); }
  .btn-danger { border: 0; background: var(--danger-color); color: white; }
  .btn-primary:disabled, .btn-secondary:disabled, .btn-danger:disabled {
    opacity: 0.55;
    cursor: not-allowed;
  }

  /* Stack on smaller screens; allow normal page scroll. */
  @media (max-width: 960px) {
    .page { overflow: auto; height: auto; max-height: none; }
    .body {
      display: flex;
      flex-direction: column;
      overflow: visible;
      height: auto;
    }
    .overview, .controls { grid-column: auto; grid-row: auto; }
    .fact-grid { grid-template-columns: 1fr; }
  }

  @media (max-width: 768px) {
    .body { padding: 12px 16px 16px; }
  }
</style>
