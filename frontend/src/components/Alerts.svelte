<script>
  import { onMount, onDestroy } from 'svelte';
  import PageHeader from './PageHeader.svelte';
  import { Alert, Badge, Button, Modal, Spinner, Tabs } from './ui/index.js';
  import {
    getAlerts,
    getAlertSummary,
    acknowledgeAlert,
    unacknowledgeAlert,
    silenceAlert,
    evaluateAlertsNow,
    getAlertSilences,
    createAlertSilence,
    deleteAlertSilence,
    getAlertTypes,
    updateAlertType,
    updateAlertSettings,
    getAlertChannels,
    createAlertChannel,
    updateAlertChannel,
    deleteAlertChannel,
    testAlertChannel,
    getAlertRecipients,
    saveAlertRecipients,
    getAlertDeliveries,
  } from '../lib/api.js';

  const SEVERITY_VARIANT = { critical: 'danger', warning: 'warning', info: 'info' };
  const SILENCE_CHOICES = [
    { minutes: 60, label: '1 hour' },
    { minutes: 240, label: '4 hours' },
    { minutes: 1440, label: '1 day' },
    { minutes: 10080, label: '1 week' },
  ];
  const GLOBAL_LABELS = {
    hold_down_seconds: 'Hold-down before resolving (seconds)',
    reminder_interval_seconds: 'Reminder interval (seconds, 0 for none)',
    reopen_window_seconds: 'Reopen window for flapping alerts (seconds)',
    flap_limit: 'Reopenings before notices pause',
    storm_threshold: 'Notices per channel before one summary is sent',
    alert_retention_days: 'Keep resolved alerts (days)',
    delivery_retention_days: 'Keep sent notifications (days)',
    failed_delivery_retention_days: 'Keep failed notifications (days)',
  };
  const STAFF_GLOBALS = Object.keys(GLOBAL_LABELS);

  let tab = 'open';
  let summary = null;
  let alerts = [];
  let total = 0;
  let loading = true;
  let error = '';
  let notice = '';
  let severityFilter = '';
  let search = '';
  let expanded = null;
  let timer = null;

  let silences = [];
  let types = [];
  let globalSettings = {};
  let channels = [];
  let recipients = [];
  let deliveries = [];
  let deliveryTotal = 0;
  let busy = false;

  let channelModal = false;
  let editing = null;
  let form = emptyForm();
  let silenceForm = { alert_type: '', resource_type: '', resource_id: '', duration_minutes: 60, reason: '' };

  function emptyForm() {
    return { name: '', kind: 'email', min_severity: 'warning', send_resolved: true, addressesText: '', url: '', secret: '', enabled: true };
  }

  $: tabs = [
    { id: 'open', label: `Open${summary ? ` (${summary.active + summary.acknowledged + summary.silenced})` : ''}` },
    { id: 'history', label: 'History' },
    { id: 'silences', label: 'Silences' },
    { id: 'settings', label: 'Alert types' },
    { id: 'channels', label: 'Channels' },
    { id: 'deliveries', label: 'Deliveries' },
  ];

  function fmt(value) {
    return value ? new Date(value).toLocaleString() : 'never';
  }

  function guard(fn) {
    return async (...args) => {
      error = '';
      notice = '';
      busy = true;
      try {
        await fn(...args);
      } catch (err) {
        error = err.message || 'Something went wrong';
      } finally {
        busy = false;
      }
    };
  }

  async function loadSummary() {
    try {
      summary = await getAlertSummary();
    } catch (err) {
      error = err.message;
    }
  }

  async function loadTab() {
    loading = true;
    error = '';
    try {
      await loadSummary();
      if (tab === 'open' || tab === 'history') {
        const data = await getAlerts({
          state: tab === 'open' ? 'open' : 'resolved',
          severity: severityFilter,
          q: search,
          limit: 100,
        });
        alerts = data.items;
        total = data.total;
      } else if (tab === 'silences') {
        silences = (await getAlertSilences()).items;
      } else if (tab === 'settings') {
        const data = await getAlertTypes();
        types = data.items;
        globalSettings = data.global;
      } else if (tab === 'channels') {
        channels = (await getAlertChannels()).items;
        recipients = (await getAlertRecipients()).items;
      } else if (tab === 'deliveries') {
        const data = await getAlertDeliveries({ limit: 100 });
        deliveries = data.items;
        deliveryTotal = data.total;
      }
    } catch (err) {
      error = err.message || 'Failed to load';
    } finally {
      loading = false;
    }
  }

  onMount(() => {
    timer = setInterval(() => {
      if (tab === 'open') loadTab();
    }, 30000);
  });
  onDestroy(() => timer && clearInterval(timer));

  const ack = guard(async (alert) => {
    const note = prompt('Note (optional)', '') ?? null;
    if (note === null) return;
    await acknowledgeAlert(alert.id, note);
    await loadTab();
  });
  const unack = guard(async (alert) => {
    await unacknowledgeAlert(alert.id);
    await loadTab();
  });
  const silenceOne = guard(async (alert, minutes) => {
    await silenceAlert(alert.id, minutes, 'Silenced from the alert list');
    notice = 'Silenced. It stays listed but sends nothing until the silence ends.';
    await loadTab();
  });
  const runChecks = guard(async () => {
    const result = await evaluateAlertsNow();
    notice = `Checks ran: ${result.opened} opened, ${result.resolved} resolved, ${result.notices} notices queued.`;
    if (result.failed_types.length) error = `Checks failed for: ${result.failed_types.join(', ')}`;
    await loadTab();
  });
  const addSilence = guard(async () => {
    const data = { duration_minutes: Number(silenceForm.duration_minutes), reason: silenceForm.reason || null };
    if (silenceForm.alert_type) data.alert_type = silenceForm.alert_type;
    if (silenceForm.resource_type) data.resource_type = silenceForm.resource_type;
    if (silenceForm.resource_id) data.resource_id = silenceForm.resource_id;
    await createAlertSilence(data);
    silenceForm = { alert_type: '', resource_type: '', resource_id: '', duration_minutes: 60, reason: '' };
    await loadTab();
  });
  const removeSilence = guard(async (s) => {
    await deleteAlertSilence(s.id);
    await loadTab();
  });

  const saveType = guard(async (t) => {
    const params = {};
    t.params.forEach((p) => (params[p.key] = Number(p.value)));
    await updateAlertType(t.type, {
      enabled: t.enabled,
      severity: t.severity,
      params,
      reminder_interval_seconds: Number(t.reminder_interval_seconds),
      hold_down_seconds: Number(t.hold_down_seconds),
    });
    notice = `Saved ${t.title}.`;
    await loadTab();
  });
  const resetType = guard(async (t) => {
    await updateAlertType(t.type, { reset: true });
    notice = `${t.title} is back to its defaults.`;
    await loadTab();
  });
  const saveGlobals = guard(async () => {
    const settings = {};
    STAFF_GLOBALS.forEach((key) => (settings[key] = Number(globalSettings[key])));
    await updateAlertSettings(settings);
    notice = 'Saved the shared settings.';
    await loadTab();
  });

  function openChannel(channel) {
    editing = channel;
    form = channel
      ? {
          name: channel.name,
          kind: channel.kind,
          min_severity: channel.min_severity,
          send_resolved: channel.send_resolved,
          addressesText: (channel.addresses || []).join('\n'),
          url: '',
          secret: '',
          enabled: channel.enabled,
        }
      : emptyForm();
    channelModal = true;
  }
  const saveChannel = guard(async () => {
    const data = {
      name: form.name,
      min_severity: form.min_severity,
      send_resolved: form.send_resolved,
      enabled: form.enabled,
    };
    if (form.kind === 'email') {
      data.addresses = form.addressesText.split(/[\s,;]+/).filter(Boolean);
    } else {
      if (form.url) data.url = form.url;
      if (form.kind === 'webhook' && form.secret) data.secret = form.secret;
    }
    if (editing) await updateAlertChannel(editing.id, data);
    else await createAlertChannel({ ...data, kind: form.kind });
    channelModal = false;
    await loadTab();
  });
  const removeChannel = guard(async (channel) => {
    if (!confirm(`Delete channel "${channel.name}"?`)) return;
    await deleteAlertChannel(channel.id);
    await loadTab();
  });
  const testChannel = guard(async (channel) => {
    const result = await testAlertChannel(channel.id);
    if (result.ok) notice = `Test notice sent through "${channel.name}".`;
    else error = `Test failed: ${result.errors.join('; ')}`;
  });
  const saveRecipients = guard(async () => {
    await saveAlertRecipients(recipients.map((r) => ({ user_id: r.user_id, enabled: r.enabled, min_severity: r.min_severity })));
    notice = 'Saved recipients.';
    await loadTab();
  });

  let loadedTab = null;
  $: if (tab !== loadedTab) {
    loadedTab = tab;
    loadTab();
  }
</script>

<PageHeader title="Alerts">
  <svelte:fragment slot="actions">
    <Button variant="secondary" on:click={runChecks} disabled={busy}>Run checks now</Button>
  </svelte:fragment>
</PageHeader>

<div class="admin-page alerts-page">
  {#if summary && !summary.has_destination}
    <Alert type="warning">No channel or staff recipient is set up, so alerts are only listed here. Add one under Channels.</Alert>
  {/if}
  {#if summary && !summary.smtp_enabled}
    <Alert type="info">SMTP is switched off, so email notices are skipped. Webhook channels still work.</Alert>
  {/if}
  {#if summary && summary.evaluation_stale}
    <Alert type="danger">
      The checks have not run recently (last run: {fmt(summary.last_evaluated_at)}). The alert worker may be stopped.
    </Alert>
  {/if}
  {#if summary && summary.failed_types.length}
    <Alert type="warning">Some checks failed on the last run: {summary.failed_types.join(', ')}.</Alert>
  {/if}
  {#if error}<Alert type="danger">{error}</Alert>{/if}
  {#if notice}<Alert type="success">{notice}</Alert>{/if}

  <Tabs {tabs} bind:active={tab} />

  {#if loading}
    <div class="state"><Spinner /><span>Loading…</span></div>
  {:else if tab === 'open' || tab === 'history'}
    <div class="filters">
      <select bind:value={severityFilter} on:change={loadTab} aria-label="Severity">
        <option value="">All severities</option>
        <option value="critical">Critical</option>
        <option value="warning">Warning</option>
        <option value="info">Info</option>
      </select>
      <input placeholder="Search" bind:value={search} on:keydown={(e) => e.key === 'Enter' && loadTab()} />
      <span class="muted">{total} {tab === 'open' ? 'open' : 'resolved'}</span>
    </div>
    {#if alerts.length === 0}
      <p class="muted empty">{tab === 'open' ? 'Nothing is wrong right now.' : 'No resolved alerts yet.'}</p>
    {:else}
      <ul class="alert-list">
        {#each alerts as alert (alert.id)}
          <li class="alert-row" class:muted-row={alert.acknowledged || alert.silenced}>
            <button class="row-head" on:click={() => (expanded = expanded === alert.id ? null : alert.id)}>
              <Badge variant={SEVERITY_VARIANT[alert.severity]}>{alert.severity}</Badge>
              <span class="title">{alert.title}</span>
              {#if alert.acknowledged}<Badge variant="muted">acknowledged</Badge>{/if}
              {#if alert.silenced}<Badge variant="muted">silenced</Badge>{/if}
              {#if alert.flap_count > 0}<Badge variant="warning">flapping x{alert.flap_count}</Badge>{/if}
              <span class="when">{tab === 'open' ? `since ${fmt(alert.opened_at)}` : `resolved ${fmt(alert.resolved_at)}`}</span>
            </button>
            {#if expanded === alert.id}
              <div class="detail">
                {#if alert.message}<p>{alert.message}</p>{/if}
                <dl>
                  <dt>Type</dt><dd>{alert.type}</dd>
                  <dt>Resource</dt><dd>{alert.resource_label || alert.resource_id}</dd>
                  <dt>Opened</dt><dd>{fmt(alert.opened_at)}</dd>
                  <dt>Last seen</dt><dd>{fmt(alert.last_seen_at)}</dd>
                  <dt>Notices sent</dt><dd>{alert.notify_count}</dd>
                  {#if alert.acknowledged}
                    <dt>Acknowledged</dt>
                    <dd>{alert.acknowledged_by || 'unknown'} at {fmt(alert.acknowledged_at)}{alert.acknowledge_note ? `: ${alert.acknowledge_note}` : ''}</dd>
                  {/if}
                </dl>
                {#if tab === 'open'}
                  <div class="actions">
                    {#if alert.acknowledged}
                      <Button variant="secondary" size="small" on:click={() => unack(alert)} disabled={busy}>Clear acknowledgement</Button>
                    {:else}
                      <Button variant="secondary" size="small" on:click={() => ack(alert)} disabled={busy}>Acknowledge</Button>
                    {/if}
                    {#each SILENCE_CHOICES as choice}
                      <Button variant="secondary" size="small" on:click={() => silenceOne(alert, choice.minutes)} disabled={busy}>
                        Silence {choice.label}
                      </Button>
                    {/each}
                  </div>
                {/if}
              </div>
            {/if}
          </li>
        {/each}
      </ul>
    {/if}
  {:else if tab === 'silences'}
    <div class="panel">
      <h3>Silence something</h3>
      <p class="muted">
        Pick an alert type, a resource (for example a switch under maintenance), or both. Silenced alerts stay listed but send nothing.
      </p>
      <div class="grid">
        <label>Alert type
          <select bind:value={silenceForm.alert_type}>
            <option value="">Any type</option>
            {#each types as t}<option value={t.type}>{t.title}</option>{/each}
          </select>
        </label>
        <label>Resource type
          <input bind:value={silenceForm.resource_type} placeholder="switch, storage_node, pdu…" />
        </label>
        <label>Resource id
          <input bind:value={silenceForm.resource_id} placeholder="optional" />
        </label>
        <label>For (minutes)
          <input type="number" min="1" bind:value={silenceForm.duration_minutes} />
        </label>
        <label class="wide">Reason
          <input bind:value={silenceForm.reason} placeholder="Planned maintenance" />
        </label>
      </div>
      <Button on:click={addSilence} disabled={busy}>Add silence</Button>
    </div>
    <h3>Active silences</h3>
    {#if silences.length === 0}
      <p class="muted">None.</p>
    {:else}
      <ul class="simple-list">
        {#each silences as s (s.id)}
          <li>
            <span>
              <b>{s.alert_type || 'any type'}</b> on <b>{s.resource_type ? `${s.resource_type} ${s.resource_id || '(all)'}` : 'any resource'}</b>
              until {fmt(s.ends_at)}{s.reason ? `: ${s.reason}` : ''}
              <small>by {s.created_by || 'unknown'}</small>
            </span>
            <Button variant="secondary" size="small" on:click={() => removeSilence(s)} disabled={busy}>End</Button>
          </li>
        {/each}
      </ul>
    {/if}
  {:else if tab === 'settings'}
    <div class="panel">
      <h3>Shared settings</h3>
      <div class="grid">
        {#each STAFF_GLOBALS as key}
          <label>{GLOBAL_LABELS[key]}<input type="number" min="0" bind:value={globalSettings[key]} /></label>
        {/each}
      </div>
      <Button on:click={saveGlobals} disabled={busy}>Save shared settings</Button>
    </div>
    {#each types as t (t.type)}
      <div class="panel type-panel">
        <div class="type-head">
          <label class="check"><input type="checkbox" bind:checked={t.enabled} /> <b>{t.title}</b></label>
          {#if t.overridden}<Badge variant="accent">customised</Badge>{/if}
        </div>
        <p class="muted">{t.description}</p>
        <div class="grid">
          <label>Severity
            <select bind:value={t.severity}>
              <option value="info">Info</option><option value="warning">Warning</option><option value="critical">Critical</option>
            </select>
          </label>
          {#each t.params as p}
            <label>{p.label} ({p.unit || 'value'})<input type="number" min={p.min} max={p.max} bind:value={p.value} /></label>
          {/each}
          <label>Reminder (seconds, 0 for none)<input type="number" min="0" bind:value={t.reminder_interval_seconds} /></label>
          <label>Hold-down (seconds)<input type="number" min="0" bind:value={t.hold_down_seconds} /></label>
        </div>
        <div class="actions">
          <Button size="small" on:click={() => saveType(t)} disabled={busy}>Save</Button>
          {#if t.overridden}<Button size="small" variant="secondary" on:click={() => resetType(t)} disabled={busy}>Reset to defaults</Button>{/if}
        </div>
      </div>
    {/each}
  {:else if tab === 'channels'}
    <div class="panel">
      <div class="type-head">
        <h3>Channels</h3>
        <Button size="small" on:click={() => openChannel(null)}>Add channel</Button>
      </div>
      {#if channels.length === 0}
        <p class="muted">No channels yet. Email, a generic webhook, Discord and Slack are supported.</p>
      {:else}
        <ul class="simple-list">
          {#each channels as c (c.id)}
            <li>
              <span>
                <b>{c.name}</b> <Badge variant="info">{c.kind}</Badge>
                {#if !c.enabled}<Badge variant="muted">off</Badge>{/if}
                <small>from {c.min_severity}{c.send_resolved ? ', with resolved notices' : ''} ·
                  {c.kind === 'email' ? c.addresses.join(', ') : c.url}</small>
              </span>
              <span class="actions">
                <Button size="small" variant="secondary" on:click={() => testChannel(c)} disabled={busy}>Test</Button>
                <Button size="small" variant="secondary" on:click={() => openChannel(c)}>Edit</Button>
                <Button size="small" variant="secondary" on:click={() => removeChannel(c)} disabled={busy}>Delete</Button>
              </span>
            </li>
          {/each}
        </ul>
      {/if}
    </div>
    <div class="panel">
      <h3>Staff recipients</h3>
      <p class="muted">Staff who receive alert email at their account address.</p>
      <ul class="simple-list">
        {#each recipients as r (r.user_id)}
          <li>
            <label class="check"><input type="checkbox" bind:checked={r.enabled} disabled={!r.email} /> <b>{r.username}</b> <small>{r.email || 'no email address'}</small></label>
            <select bind:value={r.min_severity} aria-label="Minimum severity">
              <option value="info">Info and above</option><option value="warning">Warning and above</option><option value="critical">Critical only</option>
            </select>
          </li>
        {/each}
      </ul>
      <Button on:click={saveRecipients} disabled={busy}>Save recipients</Button>
    </div>
  {:else if tab === 'deliveries'}
    <p class="muted">{deliveryTotal} recent notifications.</p>
    <table class="table">
      <thead><tr><th>When</th><th>Channel</th><th>Target</th><th>Event</th><th>Status</th><th>Tries</th><th>Error</th></tr></thead>
      <tbody>
        {#each deliveries as d (d.id)}
          <tr>
            <td>{fmt(d.created_at)}</td><td>{d.channel_kind}</td><td>{d.target}</td><td>{d.event}</td>
            <td><Badge variant={d.status === 'sent' ? 'success' : d.status === 'dead' ? 'danger' : 'muted'}>{d.status}</Badge></td>
            <td>{d.attempts}</td><td>{d.error || ''}</td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}
</div>

{#if channelModal}
<Modal title={editing ? 'Edit channel' : 'Add channel'} onClose={() => (channelModal = false)}>
  <div class="grid one">
    <label>Name<input bind:value={form.name} maxlength="128" /></label>
    {#if !editing}
      <label>Kind
        <select bind:value={form.kind}>
          <option value="email">Email</option><option value="webhook">Generic webhook (JSON)</option>
          <option value="discord">Discord</option><option value="slack">Slack</option>
        </select>
      </label>
    {/if}
    {#if form.kind === 'email'}
      <label>Addresses (one per line)<textarea rows="3" bind:value={form.addressesText}></textarea></label>
    {:else}
      <label>URL{editing ? ' (leave empty to keep the current one)' : ''}<input bind:value={form.url} placeholder="https://…" /></label>
      {#if form.kind === 'webhook'}
        <label>Signing secret (optional, adds X-Rackflow-Signature){editing ? ' (type to replace)' : ''}<input bind:value={form.secret} /></label>
      {/if}
    {/if}
    <label>Minimum severity
      <select bind:value={form.min_severity}>
        <option value="info">Info</option><option value="warning">Warning</option><option value="critical">Critical</option>
      </select>
    </label>
    <label class="check"><input type="checkbox" bind:checked={form.send_resolved} /> Send resolved notices</label>
    <label class="check"><input type="checkbox" bind:checked={form.enabled} /> Enabled</label>
  </div>
  <svelte:fragment slot="footer">
    <Button variant="secondary" on:click={() => (channelModal = false)}>Cancel</Button>
    <Button on:click={saveChannel} disabled={busy}>Save</Button>
  </svelte:fragment>
</Modal>
{/if}

<style>
  .alerts-page { display: grid; gap: 14px; }
  .state { min-height: 120px; display: grid; place-items: center; gap: 12px; }
  .filters { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  .muted { color: var(--text-secondary); font-size: 14px; }
  .empty { padding: 24px 0; text-align: center; }
  .alert-list { list-style: none; margin: 0; padding: 0; display: grid; gap: 8px; }
  .alert-row { border: 1px solid var(--border-color); border-radius: 8px; background: var(--bg-primary); }
  .muted-row { opacity: 0.7; }
  .row-head { display: flex; align-items: center; gap: 10px; width: 100%; padding: 10px 14px; background: none; border: 0; color: inherit; text-align: left; cursor: pointer; flex-wrap: wrap; }
  .title { font-weight: 600; flex: 1; min-width: 200px; }
  .when { color: var(--text-tertiary); font-size: 12px; }
  .detail { padding: 0 14px 14px; display: grid; gap: 10px; font-size: 14px; }
  dl { display: grid; grid-template-columns: max-content 1fr; gap: 4px 14px; margin: 0; }
  dt { color: var(--text-secondary); }
  dd { margin: 0; }
  .actions { display: flex; gap: 8px; flex-wrap: wrap; }
  .panel { border: 1px solid var(--border-color); border-radius: var(--radius-lg, 10px); background: var(--bg-primary); padding: 16px; display: grid; gap: 10px; }
  .type-head { display: flex; justify-content: space-between; align-items: center; gap: 10px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 12px; }
  .grid.one { grid-template-columns: 1fr; }
  .wide { grid-column: 1 / -1; }
  label { display: grid; gap: 6px; font-size: 13px; font-weight: 600; }
  .check { display: flex; align-items: center; gap: 8px; font-weight: 400; }
  input:not([type='checkbox']), select, textarea { padding: 8px 10px; border: 1px solid var(--border-color); border-radius: 8px; background: var(--bg-primary); color: var(--text-primary); }
  .simple-list { list-style: none; margin: 0; padding: 0; }
  .simple-list li { display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 10px 0; border-bottom: 1px solid var(--border-color); font-size: 14px; flex-wrap: wrap; }
  small { color: var(--text-tertiary); display: block; }
  h3 { margin: 0; font-size: 15px; }
  .table { width: 100%; border-collapse: collapse; font-size: 13px; }
  .table th, .table td { text-align: left; padding: 8px; border-bottom: 1px solid var(--border-color); }
</style>
