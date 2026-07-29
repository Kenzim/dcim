<script>
  import { onMount, onDestroy } from 'svelte';
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
    listDeploymentJobs,
    getAdminVmConsoleTypes,
    listServiceStrategyActions,
    runServiceStrategyAction,
    listServiceAvailableIps,
    reassignServiceIp,
    listVmBackups,
    createVmBackup,
    deleteVmBackup,
    restoreVmBackup,
    reinstallVmGuest,
    getVmSshKeys,
    putVmSshKeys,
    listVmTemplates,
  } from '../lib/api.js';
  import ServicePermissionsPanel from './ServicePermissionsPanel.svelte';

  export let serviceId;

  let service = null;
  let loading = true;
  let error = null;
  let busy = false;
  let statusDraft = 'pending';
  let latestJob = null;
  let jobPollTimer = null;
  let strategyActions = [];
  let actionMessage = '';
  let passwordDraft = '';
  let availableIps = [];
  let availableIpsLoading = false;
  let availableIpsLoaded = false;
  let selectedIpAllocationId = '';
  let ipMessage = '';
  let resetNetworkOnReassign = true;
  let backups = [];
  let backupJobs = [];
  let backupNotes = '';
  let backupMessage = '';
  let backupsLoading = false;
  let backupPollTimer = null;
  let sshKeysDraft = '';
  let sshKeysMessage = '';
  let showReinstallModal = false;
  let reinstallTemplateId = '';
  let reinstallSshKeys = '';
  let reinstallTemplates = [];
  // { vnc, serial } once known -- null means "not checked yet / unknown",
  // in which case a single generic button is shown and the backend picks
  // a sensible default (noVNC first) when opened.
  let consoleTypes = null;
  const JOB_ACTIVE_STATES = ['queued', 'running', 'waiting'];

  $: bothConsoleTypesAvailable = !!(consoleTypes && consoleTypes.vnc && consoleTypes.serial);

  $: guestState = service?.vm_guest_state || 'unprovisioned';
  $: prov = service?.config?.vm_provision || {};
  $: guestTone = guestToneFor(guestState);
  $: serviceTone = serviceToneFor(service?.status);
  $: jobActive = latestJob && JOB_ACTIVE_STATES.includes(latestJob.status);

  function jobStepTone(stepStatus) {
    switch (stepStatus) {
      case 'succeeded': return 'ok';
      case 'skipped': return 'muted';
      case 'running': return 'warn';
      case 'waiting': return 'warn';
      case 'failed': return 'bad';
      default: return 'muted';
    }
  }

  function jobTone(jobStatus) {
    switch (jobStatus) {
      case 'succeeded': return 'ok';
      case 'failed': return 'bad';
      case 'waiting': return 'warn';
      case 'running': return 'warn';
      case 'queued': return 'warn';
      case 'cancelled': return 'muted';
      default: return 'muted';
    }
  }

  async function loadLatestJob() {
    if (!service?.id) return;
    try {
      const jobs = await listDeploymentJobs(service.id, 1);
      latestJob = jobs && jobs.length ? jobs[0] : null;
    } catch (_) {
      // Non-fatal: job view is supplementary to the service record.
    }
    scheduleJobPoll();
  }

  function scheduleJobPoll() {
    if (jobPollTimer) {
      clearTimeout(jobPollTimer);
      jobPollTimer = null;
    }
    if (latestJob && JOB_ACTIVE_STATES.includes(latestJob.status)) {
      jobPollTimer = setTimeout(loadLatestJob, 3000);
    }
  }

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

  async function loadStrategyActions() {
    if (!service?.id) return;
    try {
      const res = await listServiceStrategyActions(service.id);
      strategyActions = res.actions || [];
    } catch (_) {
      strategyActions = [];
    }
  }

  async function runStrategy(action) {
    if (!service?.id) return;
    busy = true;
    error = null;
    actionMessage = '';
    try {
      const params = {};
      if (action.name === 'change_password') {
        if (!passwordDraft) throw new Error('Enter a new password first');
        params.password = passwordDraft;
      }
      const result = await runServiceStrategyAction(service.id, action.name, params);
      actionMessage = `${action.label}: ok`;
      if (result?.smbios?.SystemSerialNumber) {
        actionMessage += ` (serial ${result.smbios.SystemSerialNumber})`;
      }
      passwordDraft = '';
      service = await getVmService(service.id);
    } catch (e) {
      error = e.message || String(e);
    } finally {
      busy = false;
    }
  }

  async function loadAvailableIps({ force = false } = {}) {
    if (!service?.id) return;
    if (availableIpsLoaded && !force) return;
    availableIpsLoading = true;
    ipMessage = '';
    try {
      const res = await listServiceAvailableIps(service.id);
      availableIps = res.available || [];
      availableIpsLoaded = true;
      if (!selectedIpAllocationId && availableIps.length) {
        selectedIpAllocationId = String(availableIps[0].id);
      }
    } catch (e) {
      ipMessage = e.message || String(e);
      availableIps = [];
    } finally {
      availableIpsLoading = false;
    }
  }

  async function applyIpReassign() {
    if (!service?.id || !selectedIpAllocationId) return;
    const row = availableIps.find((r) => String(r.id) === String(selectedIpAllocationId));
    const ipLabel = row?.ip_address || `#${selectedIpAllocationId}`;
    const rebootNote = resetNetworkOnReassign
      && (service.vm_strategy_name || '').includes('cloudinit')
      ? ' Cloud-init guests reboot to apply the new address.'
      : '';
    if (!confirm(`Assign ${ipLabel} to this service and release the current IP?${rebootNote}`)) {
      return;
    }
    busy = true;
    error = null;
    ipMessage = '';
    try {
      const result = await reassignServiceIp(service.id, Number(selectedIpAllocationId), {
        resetNetwork: resetNetworkOnReassign,
      });
      if (result?.status === 'partial' && result?.network_error) {
        ipMessage = `IP set to ${result.vm_ip_address}, but guest network reset failed: ${result.network_error}`;
      } else {
        ipMessage = `IP reassigned to ${result.vm_ip_address}`
          + (resetNetworkOnReassign ? ' (guest network reset).' : '.');
      }
      service = await getVmService(service.id);
      selectedIpAllocationId = '';
      await loadAvailableIps({ force: true });
    } catch (e) {
      error = e.message || String(e);
    } finally {
      busy = false;
    }
  }

  async function load() {
    loading = true;
    error = null;
    try {
      service = await getVmService(serviceId);
      statusDraft = service?.status || 'pending';
      sshKeysDraft = service?.ssh_public_keys_text || '';
      await loadLatestJob();
      await loadStrategyActions();
      await loadBackups();
      if (service?.service_type === 'vm') {
        // Best-effort: leaves consoleTypes null (generic single button) on
        // failure, e.g. placement not configured or Proxmox unreachable.
        getAdminVmConsoleTypes(service.id)
          .then((types) => { consoleTypes = types; })
          .catch(() => { consoleTypes = null; });
      }
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
      sshKeysDraft = service?.ssh_public_keys_text || '';
      await loadLatestJob();
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
      navigate('/admin/services');
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

  async function openReinstallModal() {
    if (!service?.id) return;
    reinstallTemplateId = service.vm_template_id != null ? String(service.vm_template_id) : '';
    reinstallSshKeys = service.ssh_public_keys_text || '';
    reinstallTemplates = [];
    try {
      const info = await getVmSshKeys(service.id);
      reinstallSshKeys = info.ssh_public_keys_text || reinstallSshKeys;
      reinstallTemplates = info.reinstall_templates || [];
      if (!reinstallTemplates.length) {
        const all = await listVmTemplates();
        reinstallTemplates = (all || []).filter((t) => t.enabled !== false).map((t) => ({
          id: t.id,
          name: t.name,
          os_type: t.os_type,
          accepts_ssh_key: !!t.accepts_ssh_key,
        }));
      }
    } catch (_) {
      /* templates optional */
    }
    showReinstallModal = true;
  }

  function selectedReinstallAcceptsKeys() {
    const id = reinstallTemplateId ? Number(reinstallTemplateId) : null;
    if (id && reinstallTemplates.length) {
      const t = reinstallTemplates.find((x) => Number(x.id) === id);
      if (t) return !!t.accepts_ssh_key;
    }
    return !!service?.accepts_ssh_key || !!service?.needs_ssh_key_prompt;
  }

  async function confirmReinstall() {
    if (!service?.id) return;
    if (!confirm('Reinstall VM at the same VMID? Current guest disks are destroyed; backups remain available to restore.')) return;
    const data = {};
    if (reinstallTemplateId) data.vm_template_id = Number(reinstallTemplateId);
    if (selectedReinstallAcceptsKeys()) {
      data.ssh_public_keys = reinstallSshKeys;
    }
    showReinstallModal = false;
    await act((id) => reinstallVmGuest(id, data));
  }

  async function saveSshKeys() {
    if (!service?.id) return;
    busy = true;
    error = null;
    sshKeysMessage = '';
    try {
      const res = await putVmSshKeys(service.id, sshKeysDraft);
      service = await getVmService(service.id);
      sshKeysDraft = service.ssh_public_keys_text || '';
      sshKeysMessage = res.applied
        ? 'SSH keys saved and applied to the guest.'
        : 'SSH keys saved (will apply on next provision/reinstall if the guest agent is unavailable).';
    } catch (e) {
      error = e.message || String(e);
    } finally {
      busy = false;
    }
  }

  function formatBackupTime(ctime) {
    if (!ctime) return '—';
    const ms = Number(ctime) > 1e12 ? Number(ctime) : Number(ctime) * 1000;
    if (!Number.isFinite(ms)) return String(ctime);
    return new Date(ms).toLocaleString();
  }

  function formatBytes(size) {
    const n = Number(size);
    if (!Number.isFinite(n) || n <= 0) return '—';
    if (n < 1024) return `${n} B`;
    if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KiB`;
    if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MiB`;
    return `${(n / 1024 ** 3).toFixed(2)} GiB`;
  }

  function backupKindLabel(kind) {
    const k = String(kind || '').toLowerCase();
    if (k === 'platform') return 'Scheduled';
    if (k === 'client') return 'Customer';
    if (k === 'restore' || k === 'backup') return k.charAt(0).toUpperCase() + k.slice(1);
    return kind ? String(kind) : 'Backup';
  }

  function backupNotesDisplay(notes) {
    if (!notes) return '';
    return String(notes)
      .replace(/\s*[·•]\s*rf1:\S+/gu, '')
      .replace(/\s*rf1:\S+/g, '')
      .trim();
  }

  function stopBackupPoll() {
    if (backupPollTimer) {
      clearInterval(backupPollTimer);
      backupPollTimer = null;
    }
  }

  function ensureBackupPoll() {
    if (backupPollTimer || !service?.id) return;
    backupPollTimer = setInterval(() => {
      loadBackups({ quiet: true });
    }, 10000);
  }

  async function loadBackups({ quiet = false } = {}) {
    if (!service?.id) return;
    if (!quiet) {
      backupsLoading = true;
      backupMessage = '';
    }
    try {
      const res = await listVmBackups(service.id);
      backups = res.backups || [];
      backupJobs = res.jobs || [];
      if (backupJobs.length) ensureBackupPoll();
      else stopBackupPoll();
    } catch (e) {
      backups = [];
      backupJobs = [];
      if (!quiet) backupMessage = e.message || String(e);
      stopBackupPoll();
    } finally {
      if (!quiet) backupsLoading = false;
    }
  }

  async function createBackup() {
    if (!service?.id) return;
    busy = true;
    error = null;
    backupMessage = '';
    try {
      await createVmBackup(service.id, { notes: backupNotes || undefined, wait: false });
      backupNotes = '';
      backupMessage = 'Client backup started';
      await loadBackups();
    } catch (e) {
      error = e.message || String(e);
    } finally {
      busy = false;
    }
  }

  async function removeBackup(b) {
    if (!service?.id || !b?.deletable) return;
    if (!confirm('Delete this client backup?')) return;
    busy = true;
    error = null;
    try {
      await deleteVmBackup(service.id, { volid: b.volid, storage: b.storage });
      backupMessage = 'Client backup deleted';
      await loadBackups();
    } catch (e) {
      error = e.message || String(e);
    } finally {
      busy = false;
    }
  }

  async function restoreBackup(b) {
    if (!service?.id) return;
    if (!confirm(`Restore backup onto VMID ${service.proxmox_vmid}? The current guest will be overwritten.`)) return;
    busy = true;
    error = null;
    backupMessage = '';
    try {
      await restoreVmBackup(service.id, { volid: b.volid, storage: b.storage, wait: false });
      backupMessage = 'Restore started';
      await loadBackups();
      service = await getVmService(service.id);
    } catch (e) {
      error = e.message || String(e);
    } finally {
      busy = false;
    }
  }

  function openVnc(type) {
    if (!service?.id) return;
    // Opened synchronously (before any await) so browsers don't treat this
    // as a blocked pop-up: the ticket-mint + /vnc redirect happens entirely
    // server-side (see GET .../vm/vnc-popup), authenticated by the admin's
    // own session cookie. A real top-level window (vs. the in-page modal)
    // gives the console its own clipboard/focus context.
    const query = type ? `?type=${encodeURIComponent(type)}` : '';
    window.open(
      `/api/admin/services/${service.id}/vm/vnc-popup${query}`,
      `rackflow_console_${service.id}`,
      'width=1024,height=768,resizable=yes,scrollbars=yes'
    );
  }

  onMount(load);
  onDestroy(() => {
    if (jobPollTimer) clearTimeout(jobPollTimer);
    stopBackupPoll();
  });
</script>

<div class="page">
  <PageHeader title="VM Service Detail" />

  <div class="body">
    <div class="toolbar">
      <button class="btn-secondary" on:click={() => navigate('/admin/services')}>← Back to Services</button>
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
            <div class="ip-reassign">
              <p class="ip-reassign-help">
                Browse free pool IPs for this Proxmox cluster, then assign one.
                Releases the current IP and can reset guest networking
                (cloud-init Linux reboots).
              </p>
              <div class="ip-reassign-row">
                <button
                  class="btn-secondary"
                  type="button"
                  disabled={busy || availableIpsLoading}
                  on:click={() => loadAvailableIps({ force: true })}
                >
                  {availableIpsLoading ? 'Loading…' : (availableIpsLoaded ? 'Refresh IPs' : 'Browse available IPs')}
                </button>
              </div>
              {#if availableIpsLoaded}
                {#if availableIps.length === 0}
                  <p class="muted">No free IPs for this cluster. Add pool rows under VM IP allocations.</p>
                {:else}
                  <label class="field-label">
                    New IP
                    <select bind:value={selectedIpAllocationId} disabled={busy}>
                      {#each availableIps as row}
                        <option value={String(row.id)}>
                          {row.ip_address}
                          {#if row.gateway} · gw {row.gateway}{/if}
                          {#if row.bridge_name} · {row.bridge_name}{/if}
                          {#if row.batch_tag} · {row.batch_tag}{/if}
                        </option>
                      {/each}
                    </select>
                  </label>
                  <label class="check-label">
                    <input type="checkbox" bind:checked={resetNetworkOnReassign} disabled={busy} />
                    Reset guest network after assign
                  </label>
                  <div class="actions">
                    <button
                      class="btn-primary"
                      type="button"
                      disabled={busy || !selectedIpAllocationId}
                      on:click={applyIpReassign}
                    >
                      Assign IP
                    </button>
                  </div>
                {/if}
              {/if}
              {#if ipMessage}
                <p class="ok-msg">{ipMessage}</p>
              {/if}
            </div>
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

        {#if latestJob}
          <section class="panel">
            <div class="panel-head job-head">
              <div>
                <h3>Deployment job #{latestJob.id}</h3>
                <p>Strategy <code>{latestJob.strategy_name}</code>{#if jobActive} · live{/if}</p>
              </div>
              <span class="status-chip tone-{jobTone(latestJob.status)}">{latestJob.status}</span>
            </div>
            {#if latestJob.error_message}
              <div class="banner bad"><span>{latestJob.error_message}</span></div>
            {/if}
            <ol class="timeline">
              {#each latestJob.steps as step (step.id)}
                <li class="tl-item tone-{jobStepTone(step.status)}" class:current={step.position === latestJob.current_step_index && jobActive}>
                  <span class="tl-dot"></span>
                  <div class="tl-body">
                    <div class="tl-row">
                      <span class="tl-name">{step.name}</span>
                      <span class="tl-status">{step.status}</span>
                    </div>
                    {#if step.message}<p class="tl-msg">{step.message}</p>{/if}
                  </div>
                </li>
              {/each}
            </ol>
          </section>
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

        <ServicePermissionsPanel
          serviceId={service.id}
          serviceType={service.service_type || 'vm'}
          permissionSetId={service.permission_set_id}
          permissionOverrides={service.permission_overrides}
          on:saved={load}
        />
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
            {#if bothConsoleTypesAvailable}
              <button
                class="btn-secondary"
                disabled={busy || guestState !== 'running'}
                title={guestState !== 'running' ? 'VM must be running to open a console' : 'Opens in a new window'}
                on:click={() => openVnc('vnc')}
              >
                Open noVNC
              </button>
              <button
                class="btn-secondary"
                disabled={busy || guestState !== 'running'}
                title={guestState !== 'running' ? 'VM must be running to open a console' : 'Opens in a new window'}
                on:click={() => openVnc('serial')}
              >
                Open Serial Console
              </button>
            {:else}
              <button
                class="btn-secondary"
                disabled={busy || guestState !== 'running'}
                title={guestState !== 'running' ? 'VM must be running to open a console' : 'Opens in a new window'}
                on:click={() => openVnc()}
              >
                Open Console
              </button>
            {/if}
          </div>
        </section>

        {#if strategyActions.length}
          <section class="panel">
            <div class="panel-head">
              <h3>Strategy actions</h3>
              <p>Guest-agent actions declared by the provisioning strategy (password, network, SMBIOS, …).</p>
            </div>
            {#if strategyActions.some((a) => a.name === 'change_password')}
              <label class="field-label">
                New password
                <input type="password" bind:value={passwordDraft} placeholder="For change password" disabled={busy} />
              </label>
            {/if}
            <div class="actions">
              {#each strategyActions as action}
                <button class="btn-secondary" disabled={busy} on:click={() => runStrategy(action)}>
                  {action.label}
                </button>
              {/each}
            </div>
            {#if actionMessage}
              <p class="ok-msg">{actionMessage}</p>
            {/if}
          </section>
        {/if}

        {#if service.accepts_ssh_key}
          <section class="panel">
            <div class="panel-head">
              <h3>SSH public keys</h3>
              <p>One OpenSSH public key per line. Saving replaces <code>/root/.ssh/authorized_keys</code> when the guest agent is ready.</p>
            </div>
            <label class="field-label">
              Authorized keys
              <textarea
                rows="5"
                bind:value={sshKeysDraft}
                placeholder="ssh-ed25519 AAAA… comment"
                disabled={busy}
              ></textarea>
            </label>
            <div class="actions">
              <button class="btn-primary" disabled={busy} on:click={saveSshKeys}>Save SSH keys</button>
            </div>
            {#if sshKeysMessage}
              <p class="ok-msg">{sshKeysMessage}</p>
            {/if}
          </section>
        {/if}

        <section class="panel grow">
          <div class="panel-head">
            <h3>Lifecycle</h3>
            <p>Provision or recreate from the catalog template. Destroy removes the Proxmox VM but keeps this service record. Reinstall destroys then reprovisions at the same VMID.</p>
          </div>
          <div class="actions">
            <button class="btn-primary" disabled={busy} on:click={() => act((id) => provisionVmService(id))}>Provision VM</button>
            <button class="btn-secondary" disabled={busy} on:click={() => act((id) => recreateVmGuest(id))}>Recreate VM</button>
            <button class="btn-secondary" disabled={busy} on:click={openReinstallModal}>Reinstall (same VMID)</button>
            <button class="btn-secondary" disabled={busy} on:click={destroyVm}>Stop + Destroy VM</button>
          </div>
        </section>

        {#if showReinstallModal}
          <div class="modal-backdrop" role="presentation" on:click|self={() => { showReinstallModal = false; }}>
            <div class="modal" role="dialog" aria-labelledby="reinstall-title">
              <h3 id="reinstall-title">Reinstall VM</h3>
              <p class="muted">Optional OS/template change and SSH keys. Keys are optional; leave blank to keep or clear stored keys when the field is shown.</p>
              {#if reinstallTemplates.length}
                <label class="field-label">
                  Template
                  <select bind:value={reinstallTemplateId} disabled={busy}>
                    <option value="">Keep current template</option>
                    {#each reinstallTemplates as t}
                      <option value={String(t.id)}>{t.name} ({t.os_type || 'os'})</option>
                    {/each}
                  </select>
                </label>
              {/if}
              {#if selectedReinstallAcceptsKeys()}
                <label class="field-label">
                  SSH public keys
                  <textarea
                    rows="4"
                    bind:value={reinstallSshKeys}
                    placeholder="One key per line (optional)"
                    disabled={busy}
                  ></textarea>
                </label>
              {/if}
              <div class="actions">
                <button class="btn-secondary" disabled={busy} on:click={() => { showReinstallModal = false; }}>Cancel</button>
                <button class="btn-primary" disabled={busy} on:click={confirmReinstall}>Reinstall</button>
              </div>
            </div>
          </div>
        {/if}

        <section class="panel">
          <div class="panel-head">
            <h3>Backups</h3>
            <p>Scheduled backups are set in Proxmox (read-only here). Customer backups count against the product quota and are purged on terminate.</p>
          </div>
          <div class="backup-create">
            <input type="text" bind:value={backupNotes} placeholder="Optional notes" disabled={busy} />
            <button class="btn-primary" disabled={busy || backupJobs.some((j) => j.kind === 'backup')} on:click={createBackup}>Create customer backup</button>
            <button class="btn-secondary" disabled={busy || backupsLoading} on:click={() => loadBackups()}>Refresh</button>
          </div>
          {#if backupMessage}
            <p class="ok-msg">{backupMessage}</p>
          {/if}
          {#if backupJobs.length}
            <div class="backup-jobs">
              <p class="muted">Running jobs</p>
              <ul class="backup-list">
                {#each backupJobs as j}
                  <li class={`backup-kind-${j.scope || j.kind || 'unknown'}`}>
                    <div class="backup-meta">
                      <strong class={`kind-pill kind-${j.scope || j.kind}`}>{backupKindLabel(j.scope || j.kind)}</strong>
                      <span>{j.status || 'RUNNING'}</span>
                      {#if j.starttime}<span>{formatBackupTime(j.starttime)}</span>{/if}
                      {#if j.storage}<span class="mono">{j.storage}</span>{/if}
                    </div>
                  </li>
                {/each}
              </ul>
            </div>
          {/if}
          {#if backupsLoading}
            <p class="muted">Loading backups…</p>
          {:else if backups.length === 0}
            <p class="muted">No backups found for this VMID.</p>
          {:else}
            <ul class="backup-list">
              {#each backups as b}
                <li class={`backup-kind-${b.kind || 'unknown'}`} class:backup-running={!!b.running}>
                  <div class="backup-meta">
                    <strong class={`kind-pill kind-${b.kind}`}>{backupKindLabel(b.kind)}</strong>
                    <span>{formatBackupTime(b.ctime)}</span>
                    <span>{formatBytes(b.size)}</span>
                    <span class="mono" title={b.volid}>{b.storage}</span>
                    {#if b.template_name}<span class="backup-template">{b.template_name}</span>{/if}
                    {#if backupNotesDisplay(b.notes)}<span class="backup-title">{backupNotesDisplay(b.notes)}</span>{/if}
                    {#if b.running}<span class="warn-label">Running</span>{/if}
                  </div>
                  {#if !b.running}
                  <div class="actions">
                    <button class="btn-secondary" disabled={busy} on:click={() => restoreBackup(b)}>Restore</button>
                    {#if b.deletable}
                      <button class="btn-danger" disabled={busy} on:click={() => removeBackup(b)}>Delete</button>
                    {/if}
                  </div>
                  {/if}
                </li>
              {/each}
            </ul>
          {/if}
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

  .job-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
  .job-head code { font-family: var(--font-mono); font-size: 12px; }
  .status-chip {
    padding: 3px 10px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 700;
    text-transform: lowercase;
    border: 1px solid var(--border-color);
    background: var(--bg-secondary);
    white-space: nowrap;
  }
  .status-chip.tone-ok { color: var(--success-color); }
  .status-chip.tone-warn { color: var(--warning-color); }
  .status-chip.tone-bad { color: var(--danger-color); }
  .status-chip.tone-muted { color: var(--text-secondary); }

  .timeline { list-style: none; margin: 0; padding: 0; display: grid; gap: 2px; }
  .tl-item {
    position: relative;
    display: flex;
    gap: 10px;
    padding: 8px 0 8px 4px;
  }
  .tl-item .tl-dot {
    flex: 0 0 auto;
    width: 10px;
    height: 10px;
    margin-top: 5px;
    border-radius: 50%;
    background: var(--text-tertiary);
  }
  .tl-item.tone-ok .tl-dot { background: var(--success-color); }
  .tl-item.tone-warn .tl-dot { background: var(--warning-color); }
  .tl-item.tone-bad .tl-dot { background: var(--danger-color); }
  .tl-item.tone-muted .tl-dot { background: var(--text-tertiary); }
  .tl-item.current { background: color-mix(in srgb, var(--accent-color) 8%, transparent); border-radius: 8px; }
  .tl-body { flex: 1; min-width: 0; }
  .tl-row { display: flex; justify-content: space-between; gap: 8px; }
  .tl-name { font-weight: 600; font-size: 14px; color: var(--text-primary); }
  .tl-status { font-size: 12px; color: var(--text-secondary); text-transform: lowercase; }
  .tl-msg { margin: 2px 0 0; font-size: 12px; color: var(--text-tertiary); word-break: break-word; }

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
  .ok-msg { color: #0a7a32; font-size: 0.9rem; margin: 0.5rem 0 0; }
  .field-label { display: grid; gap: 0.35rem; font-size: 0.9rem; margin-bottom: 0.5rem; }
  .field-label input,
  .field-label select,
  .field-label textarea { padding: 0.35rem 0.5rem; font-family: inherit; }
  .field-label textarea { resize: vertical; width: 100%; box-sizing: border-box; }
  .ip-reassign {
    margin-top: 12px;
    padding-top: 12px;
    border-top: 1px solid var(--border-color);
    display: grid;
    gap: 8px;
  }
  .ip-reassign-help {
    margin: 0;
    font-size: 0.85rem;
    color: var(--text-secondary);
    line-height: 1.4;
  }
  .ip-reassign-row { display: flex; flex-wrap: wrap; gap: 8px; }
  .check-label {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 0.9rem;
    color: var(--text-secondary);
  }
  .modal-backdrop {
    position: fixed;
    inset: 0;
    z-index: 40;
    background: rgba(0, 0, 0, 0.45);
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 16px;
  }
  .modal {
    width: min(520px, 100%);
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: 12px;
    padding: 18px 20px;
    display: grid;
    gap: 10px;
  }
  .modal h3 { margin: 0; }
  .muted { color: var(--text-secondary); font-size: 0.9rem; }
  .backup-jobs {
    margin: 8px 0;
    padding: 8px 10px;
    border: 1px solid #e6d59a;
    border-radius: 8px;
    background: #fff9e8;
  }
  .backup-create {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
  }
  .backup-create input {
    flex: 1 1 160px;
    min-width: 140px;
    padding: 0.35rem 0.5rem;
  }
  .backup-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    gap: 10px;
  }
  .backup-list li {
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    gap: 8px;
    padding: 10px 12px;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background: var(--bg-secondary);
  }
  .backup-list li.backup-kind-platform {
    background: #eef4fb;
    border-color: #c5d7eb;
  }
  .backup-list li.backup-kind-client {
    background: #eef8f3;
    border-color: #b9dcc9;
  }
  .backup-list li.backup-running {
    border-color: #e6d59a;
    background: #fff9e8;
  }
  .warn-label {
    text-transform: uppercase;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.04em;
    color: #7a5b00;
  }
  .backup-meta {
    display: flex;
    flex-wrap: wrap;
    gap: 8px 12px;
    align-items: baseline;
    font-size: 13px;
  }
  .kind-pill {
    display: inline-flex;
    align-items: center;
    padding: 2px 8px;
    border-radius: 999px;
    font-size: 11px;
    font-weight: 750;
    letter-spacing: 0.02em;
    text-transform: none;
  }
  .kind-platform { background: #d9e7f7; color: #1f4b7a; }
  .kind-client { background: #d5ebe0; color: #1f5c40; }
  .backup-template {
    display: inline-flex;
    align-items: center;
    padding: 2px 8px;
    border-radius: 6px;
    background: rgba(28, 36, 48, 0.06);
    color: var(--text-secondary);
    font-size: 12px;
    font-weight: 600;
  }
  .backup-title {
    font-weight: 700;
    color: var(--text-primary);
  }
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
