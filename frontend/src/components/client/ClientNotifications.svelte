<script>
  import { onMount } from 'svelte';
  import { Alert, Button, Spinner } from '../ui/index.js';
  import { clientGetNotifications, clientSaveNotifications, clientTestNotification } from '../../lib/api.js';

  export let impersonating = false;

  let prefs = null;
  let categories = {};
  let emailEnabled = true;
  let webhookUrl = '';
  let loading = true;
  let working = false;
  let error = '';
  let success = '';
  let newSecret = '';

  $: canManage = prefs && prefs.can_manage && !impersonating;

  onMount(load);

  function apply(data) {
    prefs = data;
    categories = { ...data.categories };
    emailEnabled = data.email_enabled;
    webhookUrl = '';
  }

  async function load() {
    loading = true;
    error = '';
    try {
      apply(await clientGetNotifications());
    } catch (err) {
      error = err.message || 'Failed to load notification settings';
    } finally {
      loading = false;
    }
  }

  async function run(fn, done) {
    working = true;
    error = '';
    success = '';
    try {
      await fn();
      success = done;
    } catch (err) {
      error = err.message || 'Something went wrong';
    } finally {
      working = false;
    }
  }

  const save = () =>
    run(async () => {
      const body = { categories, email_enabled: emailEnabled };
      if (webhookUrl.trim()) body.webhook_url = webhookUrl.trim();
      const result = await clientSaveNotifications(body);
      if (result.webhook?.secret) newSecret = result.webhook.secret;
      apply(result);
    }, 'Notification settings saved.');

  const removeWebhook = () =>
    run(async () => {
      apply(await clientSaveNotifications({ webhook_url: '' }));
      newSecret = '';
    }, 'Webhook removed.');

  const rotateSecret = () =>
    run(async () => {
      const result = await clientSaveNotifications({ regenerate_secret: true });
      newSecret = result.webhook?.secret || '';
      apply(result);
    }, 'New signing secret created. Copy it now.');

  const sendTest = () => run(() => clientTestNotification(), 'A test notification is on its way.');

  const GROUPS = [
    { title: 'Your account', keys: ['sign_in', 'failed_sign_in', 'two_factor', 'password', 'mcp_keys', 'sftp_login'] },
    { title: 'Your services', keys: ['service_suspended', 'service_terminated', 'storage_quota', 'backup', 'reinstall'] },
  ];
  $: byKey = prefs ? Object.fromEntries(prefs.catalogue.map((c) => [c.key, c])) : {};
</script>

<div class="panel">
  <h2>Notifications</h2>
  <p class="muted">
    Choose what we tell you about. Nothing is sent until you turn it on. Notices only ever cover your own account and services.
  </p>

  {#if error}<Alert type="danger">{error}</Alert>{/if}
  {#if success}<Alert type="success">{success}</Alert>{/if}

  {#if loading}
    <div class="state"><Spinner /><span>Loading…</span></div>
  {:else if prefs}
    {#if !canManage}
      <p class="muted">Notification settings cannot be changed while signed in on the client's behalf.</p>
    {/if}

    {#each GROUPS as group}
      <fieldset disabled={!canManage}>
        <legend>{group.title}</legend>
        {#each group.keys as key}
          {#if byKey[key]}
            <label class="check">
              <input type="checkbox" bind:checked={categories[key]} />
              <span><b>{byKey[key].label}</b>: {byKey[key].description}</span>
            </label>
          {/if}
        {/each}
      </fieldset>
    {/each}

    <h3>How to reach you</h3>
    <label class="check">
      <input type="checkbox" bind:checked={emailEnabled} disabled={!canManage} />
      <span>Send email to the address on your account</span>
    </label>
    <label>
      Webhook URL (optional, https only)
      <input bind:value={webhookUrl} placeholder={prefs.webhook.configured ? `Current: ${prefs.webhook.url}` : 'https://…'} disabled={!canManage} />
    </label>
    {#if prefs.webhook.configured}
      <p class="muted">
        A webhook is set ({prefs.webhook.url}). Each request is signed with <code>X-Rackflow-Signature</code>.
        {#if prefs.webhook.error}<b>{prefs.webhook.error}.</b>{/if}
      </p>
      {#if canManage}
        <div class="actions">
          <Button variant="secondary" on:click={rotateSecret} disabled={working}>New signing secret</Button>
          <Button variant="secondary" on:click={removeWebhook} disabled={working}>Remove webhook</Button>
        </div>
      {/if}
    {/if}
    {#if newSecret}
      <div class="new-key">
        <strong>Your signing secret (shown once)</strong>
        <code class="secret">{newSecret}</code>
        <Button variant="secondary" on:click={() => (newSecret = '')}>I have saved it</Button>
      </div>
    {/if}

    {#if canManage}
      <div class="actions">
        <Button on:click={save} disabled={working}>Save</Button>
        <Button variant="secondary" on:click={sendTest} disabled={working}>Send a test notification</Button>
      </div>
    {/if}
  {/if}
</div>

<style>
  .panel { margin-top: 16px; border: 1px solid var(--border-color); border-radius: var(--radius-lg); background: var(--portal-card-bg, var(--bg-primary)); padding: 20px; box-shadow: var(--shadow-sm); display: grid; gap: 12px; }
  h2 { margin: 0; font-size: 17px; }
  h3 { margin: 12px 0 0; font-size: 15px; }
  .state { min-height: 120px; display: grid; place-items: center; gap: 12px; }
  label { display: grid; gap: 6px; font-size: 13px; font-weight: 600; }
  input:not([type='checkbox']) { padding: 9px 10px; border: 1px solid var(--border-color); border-radius: 8px; background: var(--bg-primary); color: var(--text-primary); }
  fieldset { border: 1px solid var(--border-color); border-radius: 8px; margin: 0; padding: 10px 14px; display: grid; gap: 8px; }
  legend { font-size: 13px; font-weight: 600; padding: 0 6px; }
  .check { display: flex; align-items: flex-start; gap: 8px; font-weight: 400; }
  .actions { display: flex; gap: 10px; flex-wrap: wrap; }
  .muted { color: var(--text-secondary); font-size: 14px; margin: 0; }
  .new-key { border: 1px solid var(--accent-color); border-radius: 8px; padding: 14px; display: grid; gap: 10px; }
  .secret { word-break: break-all; font-size: 13px; user-select: all; }
  code { font-size: 12px; }
</style>
