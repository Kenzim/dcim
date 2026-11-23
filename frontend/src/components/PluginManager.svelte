<script>
  // Admin management of installed plugins (docs/plugin-system.md section 4).
  import { onMount } from 'svelte';
  import {
    listPluginPackages,
    installPlugin,
    upgradePlugin,
    setPluginEnabled,
    uninstallPlugin,
    resetPluginHealth,
    getPluginSettings,
    savePluginSettings,
    getPluginLogs,
    getPluginDeliveries,
    retryPluginDelivery,
    setPluginAddonRefs,
  } from '../lib/api.js';

  const MASK = '\u2022'.repeat(8);
  const STATUS_LABELS = {
    enabled: 'Enabled',
    disabled: 'Disabled',
    failed: 'Failed',
    files_changed: 'Files changed',
  };
  const HEALTH_LABELS = {
    ok: 'Healthy',
    degraded: 'Recent errors',
    circuit_open: 'Paused after errors',
    failed: 'Failed',
    disabled: 'Not running',
  };
  const WEEKDAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'];

  let data = null;
  let loading = true;
  let error = '';
  let notice = '';
  let busy = '';

  // Install or upgrade review: { mode, plugin, permissions, directory }
  let review = null;
  let enableAfterInstall = true;

  let openId = '';
  let tab = 'overview';
  let settings = null;
  let settingsValues = {};
  let settingsError = '';
  let logs = [];
  let logLevel = '';
  let deliveries = [];
  let addonText = '';
  let keepData = false;

  async function load() {
    try {
      data = await listPluginPackages();
      error = '';
    } catch (e) {
      error = e.message;
    } finally {
      loading = false;
    }
  }

  onMount(load);

  $: installable = (data?.available || []).filter((p) => !p.installed);
  $: openPlugin = (data?.installed || []).find((p) => p.id === openId) || null;

  function riskClass(risk) {
    return risk === 'high' ? 'risk-high' : risk === 'medium' ? 'risk-medium' : 'risk-low';
  }

  function when(value) {
    return value ? new Date(value).toLocaleString('en-GB') : 'Never';
  }

  async function act(key, fn, success) {
    busy = key;
    notice = '';
    error = '';
    try {
      await fn();
      if (success) notice = success;
      await load();
    } catch (e) {
      error = e.message;
    } finally {
      busy = '';
    }
  }

  function startInstall(pkg) {
    review = { mode: 'install', plugin: pkg, permissions: pkg.permissions, directory: pkg.directory };
    enableAfterInstall = true;
  }

  function startUpgrade(plugin) {
    review = { mode: 'upgrade', plugin, permissions: plugin.upgrade.added_permissions, removed: plugin.upgrade.removed_permissions };
  }

  async function confirmReview() {
    const r = review;
    const keys = r.permissions.map((p) => p.key);
    if (r.mode === 'install') {
      await act('review', () => installPlugin(r.directory, keys, enableAfterInstall), `${r.plugin.name} installed.`);
    } else {
      await act('review', () => upgradePlugin(r.plugin.id, keys), `${r.plugin.name} upgraded to ${r.plugin.upgrade.version}.`);
    }
    if (!error) review = null;
  }

  async function open(plugin, nextTab = 'overview') {
    openId = openId === plugin.id && nextTab === tab ? '' : plugin.id;
    tab = nextTab;
    keepData = false;
    addonText = (plugin.addon_refs || []).join(', ');
    if (openId) await loadTab();
  }

  async function loadTab() {
    settingsError = '';
    try {
      if (tab === 'settings') {
        settings = await getPluginSettings(openId);
        settingsValues = { ...settings.values };
        for (const field of settings.schema) {
          if (field.type === 'window' && !settingsValues[field.key]) {
            settingsValues[field.key] = { days: [], start: '02:00', end: '05:00', timezone: 'Europe/London' };
          }
          if (field.type === 'secret') settingsValues[field.key] = settings.secrets_set[field.key] ? MASK : '';
        }
      } else if (tab === 'logs') {
        logs = (await getPluginLogs(openId, logLevel)).items;
      } else if (tab === 'deliveries') {
        deliveries = (await getPluginDeliveries(openId)).items;
      }
    } catch (e) {
      settingsError = e.message;
    }
  }

  function switchTab(next) {
    tab = next;
    loadTab();
  }

  async function saveSettings() {
    busy = 'settings';
    settingsError = '';
    try {
      const values = {};
      for (const field of settings.schema) {
        let v = settingsValues[field.key];
        if ((field.type === 'integer' || field.type === 'number') && v === '') v = null;
        values[field.key] = v;
      }
      settings = await savePluginSettings(openId, values);
      notice = 'Settings saved.';
      await loadTab();
    } catch (e) {
      settingsError = e.message;
    } finally {
      busy = '';
    }
  }

  function toggleDay(key, day) {
    const w = settingsValues[key];
    w.days = w.days.includes(day) ? w.days.filter((d) => d !== day) : [...w.days, day];
    settingsValues = { ...settingsValues };
  }

  async function saveAddons() {
    const refs = addonText.split(/[\s,]+/).map((s) => s.trim()).filter(Boolean);
    await act('addons', () => setPluginAddonRefs(openId, refs), 'Add-on links saved.');
  }

  async function retry(id) {
    await act('retry', () => retryPluginDelivery(openId, id), 'Delivery queued again.');
    await loadTab();
  }

  async function remove(plugin) {
    const what = keepData ? 'Its settings and stored data are kept.' : 'Its settings and stored data are deleted.';
    if (!window.confirm(`Remove ${plugin.name}? ${what}`)) return;
    await act('remove', () => uninstallPlugin(plugin.id, keepData), `${plugin.name} removed.`);
    openId = '';
  }
</script>

<section class="manager">
  <div class="manager-head">
    <div>
      <h2>Installed plugins</h2>
      <p class="note">
        Plugins are Python packages an administrator installs from the plugins directory
        {#if data}(<code>{data.plugins_dir}</code>, plugin API {data.api_version}){/if}.
        They run inside Rackflow with the permissions you approve.
      </p>
    </div>
  </div>

  {#if error}<div class="msg msg-error">{error}</div>{/if}
  {#if notice}<div class="msg msg-ok">{notice}</div>{/if}

  {#if loading}
    <p class="note">Loading plugins…</p>
  {:else if data}
    {#if !data.installed.length}
      <p class="note">No plugins are installed.</p>
    {/if}

    {#each data.installed as plugin (plugin.id)}
      <div class="card">
        <div class="card-head">
          <div>
            <strong>{plugin.name}</strong> <span class="muted">v{plugin.version}</span>
            <span class="pill status-{plugin.status}">{STATUS_LABELS[plugin.status] || plugin.status}</span>
            {#if plugin.status === 'enabled'}
              <span class="pill health-{plugin.health.state}">{HEALTH_LABELS[plugin.health.state] || plugin.health.state}</span>
            {/if}
            {#if plugin.upgrade}<span class="pill status-files_changed">Upgrade available: {plugin.upgrade.version}</span>{/if}
            <div class="muted small">{plugin.description}</div>
            {#if plugin.disabled_reason}<div class="small text-danger">{plugin.disabled_reason}</div>{/if}
          </div>
          <div class="actions">
            {#if plugin.upgrade}
              <button class="btn btn-small btn-primary" disabled={!!busy} on:click={() => startUpgrade(plugin)}>Review upgrade</button>
            {/if}
            {#if plugin.status === 'enabled'}
              <button class="btn btn-small btn-secondary" disabled={!!busy}
                on:click={() => act('toggle', () => setPluginEnabled(plugin.id, false), `${plugin.name} disabled.`)}>Disable</button>
            {:else if plugin.status !== 'files_changed'}
              <button class="btn btn-small btn-primary" disabled={!!busy}
                on:click={() => act('toggle', () => setPluginEnabled(plugin.id, true), `${plugin.name} enabled.`)}>Enable</button>
            {/if}
            <button class="btn btn-small btn-secondary" on:click={() => open(plugin)}>{openId === plugin.id ? 'Close' : 'Manage'}</button>
          </div>
        </div>

        {#if openId === plugin.id}
          <div class="tabs">
            {#each [['overview', 'Overview'], ['settings', 'Settings'], ['logs', 'Log'], ['deliveries', 'Events']] as [id, label]}
              <button class="tab" class:active={tab === id} on:click={() => switchTab(id)}>{label}</button>
            {/each}
          </div>

          {#if settingsError}<div class="msg msg-error">{settingsError}</div>{/if}

          {#if tab === 'overview'}
            <div class="grid">
              <div>
                <h4>Permissions granted</h4>
                <ul class="perms">
                  {#each plugin.permissions as perm (perm.key)}
                    <li><span class="pill {riskClass(perm.risk)}">{perm.risk}</span> <strong>{perm.label}</strong>: {perm.description}</li>
                  {:else}
                    <li class="muted">None</li>
                  {/each}
                </ul>
              </div>
              <div>
                <h4>Health</h4>
                <dl class="kv">
                  <dt>Failures in a row</dt><dd>{plugin.health.consecutive_failures}</dd>
                  <dt>Failures in total</dt><dd>{plugin.health.total_failures}</dd>
                  <dt>Last success</dt><dd>{when(plugin.health.last_success_at)}</dd>
                  <dt>Last error</dt><dd>{plugin.health.last_error ? `${when(plugin.health.last_error_at)}: ${plugin.health.last_error}` : 'None'}</dd>
                  {#if plugin.health.circuit_open_until}<dt>Paused until</dt><dd>{when(plugin.health.circuit_open_until)}</dd>{/if}
                  <dt>Events waiting</dt><dd>{plugin.health.queued_deliveries}</dd>
                  <dt>Events failed for good</dt><dd>{plugin.health.dead_deliveries}</dd>
                </dl>
                <button class="btn btn-small btn-secondary" disabled={!!busy}
                  on:click={() => act('health', () => resetPluginHealth(plugin.id), 'Health counters reset.')}>Reset health</button>
              </div>
              <div>
                <h4>What it uses</h4>
                <dl class="kv">
                  <dt>Services</dt>
                  <dd>{plugin.services ? `${plugin.services.types.join(', ')} (${plugin.services.enablement === 'all' ? 'all services' : plugin.services.enablement === 'addon' ? 'services with a linked add-on' : 'chosen services'})` : 'None'}</dd>
                  <dt>Events</dt><dd>{plugin.events.join(', ') || 'None'}</dd>
                  <dt>Checks before</dt><dd>{plugin.hooks.join(', ') || 'None'}</dd>
                  <dt>Schedules</dt><dd>{plugin.schedules.map((s) => `${s.id} every ${s.every}`).join(', ') || 'None'}</dd>
                  <dt>Buttons</dt><dd>{plugin.actions.map((a) => `${a.label} (${a.audience.join(', ')})`).join('; ') || 'None'}</dd>
                  <dt>MCP tools</dt><dd>{plugin.mcp_tools.map((t) => `${t.id} (${t.audience})`).join(', ') || 'None'}</dd>
                </dl>
              </div>
              {#if plugin.services}
                <div>
                  <h4>Product add-ons</h4>
                  <p class="small muted">Services that buy one of these add-ons get this plugin. Use <code>whmcs:ID</code> for WHMCS add-ons or <code>store:ID</code> for Rackflow store add-ons.</p>
                  <input class="input" bind:value={addonText} placeholder="whmcs:12, store:3" />
                  <button class="btn btn-small btn-secondary" disabled={!!busy} on:click={saveAddons}>Save add-on links</button>
                </div>
              {/if}
            </div>
            <div class="danger-zone">
              <label class="small"><input type="checkbox" bind:checked={keepData} /> Keep settings and stored data</label>
              <button class="btn btn-small btn-danger" disabled={!!busy} on:click={() => remove(plugin)}>Remove plugin</button>
            </div>
          {:else if tab === 'settings' && settings}
            {#if !settings.schema.length}
              <p class="muted">This plugin has no settings.</p>
            {:else}
              <form class="settings" on:submit|preventDefault={saveSettings}>
                {#each settings.schema as field (field.key)}
                  <label class="field">
                    <span>{field.label}{field.required ? ' *' : ''}</span>
                    {#if field.type === 'boolean'}
                      <input type="checkbox" bind:checked={settingsValues[field.key]} />
                    {:else if field.type === 'integer' || field.type === 'number'}
                      <input class="input" type="number" bind:value={settingsValues[field.key]} min={field.min ?? undefined} max={field.max ?? undefined} step={field.type === 'integer' ? 1 : 'any'} />
                    {:else if field.type === 'select'}
                      <select class="input" bind:value={settingsValues[field.key]}>
                        {#each field.options || [] as o (String(o.value))}<option value={o.value}>{o.label}</option>{/each}
                      </select>
                    {:else if field.type === 'text'}
                      <textarea class="input" bind:value={settingsValues[field.key]}></textarea>
                    {:else if field.type === 'secret'}
                      <input class="input" type="password" autocomplete="new-password" bind:value={settingsValues[field.key]}
                        placeholder={settings.secrets_set[field.key] ? 'Saved; type to replace, clear to remove' : 'Not set'} />
                    {:else if field.type === 'window'}
                      <div class="window">
                        {#each WEEKDAYS as day}
                          <label class="day"><input type="checkbox" checked={settingsValues[field.key].days.includes(day)} on:change={() => toggleDay(field.key, day)} />{day}</label>
                        {/each}
                        <input class="input short" type="time" bind:value={settingsValues[field.key].start} />
                        <span>to</span>
                        <input class="input short" type="time" bind:value={settingsValues[field.key].end} />
                        <input class="input" bind:value={settingsValues[field.key].timezone} placeholder="Europe/London" />
                      </div>
                    {:else if field.type === 'url'}
                      <input class="input" type="url" bind:value={settingsValues[field.key]} />
                    {:else}
                      <input class="input" type="text" bind:value={settingsValues[field.key]} />
                    {/if}
                    {#if field.help}<small class="muted">{field.help}</small>{/if}
                  </label>
                {/each}
                <button class="btn btn-primary" type="submit" disabled={busy === 'settings'}>Save settings</button>
              </form>
            {/if}
          {:else if tab === 'logs'}
            <div class="toolbar">
              <select class="input short" bind:value={logLevel} on:change={loadTab}>
                <option value="">All levels</option>
                <option value="error">Errors</option>
                <option value="warning">Warnings</option>
                <option value="info">Information</option>
                <option value="debug">Debug</option>
              </select>
              <button class="btn btn-small btn-secondary" on:click={loadTab}>Refresh</button>
            </div>
            <table class="table">
              <thead><tr><th>Time</th><th>Level</th><th>Service</th><th>Message</th></tr></thead>
              <tbody>
                {#each logs as row (row.id)}
                  <tr><td>{when(row.created_at)}</td><td>{row.level}</td><td>{row.service_id ?? ''}</td><td class="wrap">{row.message}</td></tr>
                {:else}
                  <tr><td colspan="4" class="muted">No log entries.</td></tr>
                {/each}
              </tbody>
            </table>
          {:else if tab === 'deliveries'}
            <table class="table">
              <thead><tr><th>Created</th><th>Event</th><th>Service</th><th>Status</th><th>Attempts</th><th>Error</th><th></th></tr></thead>
              <tbody>
                {#each deliveries as row (row.id)}
                  <tr>
                    <td>{when(row.created_at)}</td><td>{row.event}</td><td>{row.service_id ?? ''}</td><td>{row.status}</td>
                    <td>{row.attempts}</td><td class="wrap">{row.last_error || ''}</td>
                    <td>{#if row.status === 'dead' || row.status === 'failed'}<button class="btn btn-small btn-secondary" on:click={() => retry(row.id)}>Retry</button>{/if}</td>
                  </tr>
                {:else}
                  <tr><td colspan="7" class="muted">No events delivered yet.</td></tr>
                {/each}
              </tbody>
            </table>
          {/if}
        {/if}
      </div>
    {/each}

    <h2 class="sub">Available to install</h2>
    {#each installable as pkg (`${pkg.bundled ? 'bundled' : 'plugins'}:${pkg.directory}`)}
      <div class="card">
        <div class="card-head">
          <div>
            <strong>{pkg.name}</strong> {#if pkg.version}<span class="muted">v{pkg.version}</span>{/if}
            {#if pkg.author}<span class="muted small">by {pkg.author}</span>{/if}
            <div class="muted small">{pkg.description}</div>
            {#each pkg.errors as err}<div class="small text-danger">{err}</div>{/each}
          </div>
          <div class="actions">
            <button class="btn btn-small btn-primary" disabled={!pkg.valid || !!busy} on:click={() => startInstall(pkg)}>Review and install</button>
          </div>
        </div>
      </div>
    {:else}
      <p class="note">No other packages found in the plugins directory.</p>
    {/each}
  {/if}
</section>

{#if review}
  <div class="overlay" role="presentation" on:click|self={() => (review = null)}>
    <div class="dialog" role="dialog" aria-modal="true" aria-labelledby="review-title">
      <h3 id="review-title">
        {review.mode === 'install' ? `Install ${review.plugin.name}` : `Upgrade ${review.plugin.name} to ${review.plugin.upgrade.version}`}
      </h3>
      {#if review.mode === 'upgrade' && review.plugin.upgrade.error}
        <div class="msg msg-error">{review.plugin.upgrade.error}</div>
      {/if}
      <p class="small">
        {#if review.mode === 'install'}
          This plugin runs inside Rackflow as trusted code. It asks for these permissions:
        {:else if review.permissions.length}
          The new version asks for these additional permissions:
        {:else}
          The new version asks for no new permissions.
        {/if}
      </p>
      <ul class="perms">
        {#each review.permissions as perm (perm.key)}
          <li><span class="pill {riskClass(perm.risk)}">{perm.risk}</span> <strong>{perm.label}</strong>: {perm.description}</li>
        {/each}
      </ul>
      {#if review.removed?.length}
        <p class="small">It no longer needs: {review.removed.map((p) => p.label).join(', ')}.</p>
      {/if}
      {#if review.mode === 'install'}
        <label class="small"><input type="checkbox" bind:checked={enableAfterInstall} /> Enable straight away</label>
      {/if}
      <div class="dialog-actions">
        <button class="btn btn-secondary" on:click={() => (review = null)}>Cancel</button>
        <button class="btn btn-primary" disabled={busy === 'review'} on:click={confirmReview}>
          {review.permissions.length ? 'Grant and ' : ''}{review.mode === 'install' ? 'install' : 'upgrade'}
        </button>
      </div>
    </div>
  </div>
{/if}

<style>
  .manager h2 { margin: 0 0 4px; font-size: 18px; }
  .manager h2.sub { margin-top: 24px; }
  .manager h4 { margin: 0 0 8px; font-size: 14px; }
  .note { margin: 0 0 16px; color: var(--text-secondary); font-size: 14px; }
  .muted { color: var(--text-secondary); }
  .small { font-size: 13px; }
  .text-danger { color: #b91c1c; }
  .card { background: var(--bg-primary); border: 1px solid var(--border-color); border-radius: 10px; padding: 16px; margin-bottom: 12px; }
  .card-head { display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; }
  .actions { display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
  .pill { display: inline-block; padding: 1px 8px; margin-left: 6px; border-radius: 999px; font-size: 11px; font-weight: 650; background: #f3f4f6; color: #374151; }
  .status-enabled, .health-ok, .risk-low { background: #dcfce7; color: #166534; }
  .status-files_changed, .health-degraded, .risk-medium { background: #fef3c7; color: #92400e; }
  .status-failed, .health-circuit_open, .health-failed, .risk-high { background: #fee2e2; color: #991b1b; }
  .tabs { display: flex; gap: 4px; margin: 16px 0 12px; border-bottom: 1px solid var(--border-color); }
  .tab { background: none; border: 0; padding: 8px 12px; cursor: pointer; color: var(--text-secondary); border-bottom: 2px solid transparent; }
  .tab.active { color: var(--text-primary); border-bottom-color: var(--accent-color, #2563eb); }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 20px; }
  .perms { margin: 0; padding: 0; list-style: none; display: grid; gap: 6px; font-size: 13px; }
  .perms .pill { margin-left: 0; }
  .kv { display: grid; grid-template-columns: max-content 1fr; gap: 4px 12px; margin: 0 0 8px; font-size: 13px; }
  .kv dt { color: var(--text-secondary); }
  .kv dd { margin: 0; word-break: break-word; }
  .danger-zone { display: flex; gap: 12px; align-items: center; justify-content: flex-end; margin-top: 16px; }
  .settings { display: grid; gap: 12px; max-width: 560px; }
  .field { display: grid; gap: 4px; font-size: 14px; }
  .input { padding: 6px 8px; border: 1px solid var(--border-color); border-radius: 6px; background: var(--bg-primary); color: var(--text-primary); }
  .input.short { width: auto; }
  .window { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .day { display: inline-flex; gap: 2px; align-items: center; font-size: 12px; text-transform: capitalize; }
  .toolbar { display: flex; gap: 8px; margin-bottom: 8px; }
  .table { width: 100%; border-collapse: collapse; font-size: 13px; }
  .table th, .table td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--border-color); vertical-align: top; }
  .wrap { word-break: break-word; white-space: pre-wrap; }
  .msg { padding: 8px 12px; border-radius: 6px; margin-bottom: 12px; font-size: 14px; }
  .msg-error { background: #fee2e2; color: #991b1b; }
  .msg-ok { background: #dcfce7; color: #166534; }
  .overlay { position: fixed; inset: 0; background: rgba(0, 0, 0, 0.4); display: flex; align-items: center; justify-content: center; z-index: 1000; }
  .dialog { background: var(--bg-primary); border-radius: 10px; padding: 20px; width: min(560px, 92vw); max-height: 85vh; overflow: auto; display: grid; gap: 12px; }
  .dialog h3 { margin: 0; }
  .dialog-actions { display: flex; justify-content: flex-end; gap: 8px; }
</style>
