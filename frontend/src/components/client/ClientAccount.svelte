<script>
  import { onMount } from 'svelte';
  import { Alert, Badge, Button, Spinner, Tabs } from '../ui/index.js';
  import TwoFactorPanel from '../TwoFactorPanel.svelte';
  import ClientMcpKeys from './ClientMcpKeys.svelte';
  import ClientNotifications from './ClientNotifications.svelte';
  import ClientCreditPanel from '../billing/client/ClientCreditPanel.svelte';
  import { clientGetCredit } from '../../lib/billingClientApi.js';
  import { TAX_ID_TYPE_OPTIONS, taxIdStatusInfo } from '../../lib/billingTaxFormat.js';
  import {
    clientCommerceDiscordAuthorizeUrl,
    clientCommerceDiscordUnlink,
    clientCommerceGetProfile,
    clientCommerceListActivity,
    clientCommerceListEmails,
    clientCommerceUpdateProfile,
  } from '../../lib/api.js';

  export let impersonating = false;
  /** 'profile' | 'credit' | ... ; 'credit' only applies while account credit is on. */
  export let initialTab = 'profile';

  let tab = initialTab;
  let credit = null;
  let profile = {};
  let activity = [];
  let emails = [];
  let loading = true;
  let working = false;
  let error = '';
  let success = '';
  /** The tax ID and country as last loaded or saved: changing either makes the save run a check. */
  let savedTaxId = '';
  let savedCountry = '';

  $: taxChanged = (profile.tax_id || '').trim() !== savedTaxId || (profile.country || '').toUpperCase() !== savedCountry;
  $: checking = working && taxChanged && Boolean((profile.tax_id || '').trim());
  $: taxStatus = taxIdStatusInfo(profile.tax_id_status || 'unchecked', Boolean(profile.tax_id));

  function rememberTax(saved) {
    savedTaxId = (saved?.tax_id || '').trim();
    savedCountry = (saved?.country || '').toUpperCase();
  }

  onMount(load);

  async function load() {
    loading = true;
    error = '';
    try {
      [profile, activity, emails, credit] = await Promise.all([
        clientCommerceGetProfile().catch(() => ({})),
        clientCommerceListActivity().catch(() => []),
        clientCommerceListEmails().catch(() => []),
        loadCredit(),
      ]);
      rememberTax(profile);
      if (tab === 'credit' && !credit) tab = 'profile';
    } catch (err) {
      error = err.message || 'Failed to load account data';
    } finally {
      loading = false;
    }
  }

  /** The credit summary when account credit is on for this account, else null. */
  async function loadCredit() {
    try {
      const summary = await clientGetCredit();
      return summary.enabled ? summary : null;
    } catch (_) {
      return null;
    }
  }

  async function refreshCredit() {
    credit = await loadCredit();
  }

  async function saveProfile() {
    const wasChecked = taxChanged && Boolean((profile.tax_id || '').trim());
    working = true;
    error = '';
    success = '';
    try {
      profile = await clientCommerceUpdateProfile({
        legal_name: profile.legal_name || null,
        company: profile.company || null,
        address_line1: profile.address_line1 || null,
        city: profile.city || null,
        region: profile.region || null,
        postal_code: profile.postal_code || null,
        country: profile.country || null,
        phone: profile.phone || null,
        tax_id: profile.tax_id?.trim() || null,
        tax_id_type: profile.tax_id_type || null,
        is_business: Boolean(profile.is_business),
        invoice_email: profile.invoice_email || null,
      });
      rememberTax(profile);
      success = wasChecked
        ? `Profile saved. ${taxIdStatusInfo(profile.tax_id_status || 'unchecked', true).text}`
        : 'Profile saved.';
    } catch (err) {
      error = err.message || 'Failed to save profile';
    } finally {
      working = false;
    }
  }

  async function linkDiscord() {
    if (impersonating) {
      error = 'Discord linking is disabled during impersonation.';
      return;
    }
    working = true;
    error = '';
    try {
      const { authorize_url, state } = await clientCommerceDiscordAuthorizeUrl();
      sessionStorage.setItem('rf_discord_state', state);
      window.location.href = authorize_url;
    } catch (err) {
      error = err.message || 'Discord is not configured';
    } finally {
      working = false;
    }
  }

  async function unlinkDiscord() {
    if (impersonating) return;
    working = true;
    try {
      await clientCommerceDiscordUnlink();
      success = 'Discord unlinked.';
    } catch (err) {
      error = err.message || 'Failed to unlink Discord';
    } finally {
      working = false;
    }
  }

</script>

<section aria-labelledby="account-title">
  <div class="page-head">
    <div>
      <h1 id="account-title">Account</h1>
      <p class="sub">Billing profile, security, activity, notifications, and MCP keys.</p>
    </div>
  </div>

  <Tabs
    tabs={[
      { id: 'profile', label: 'Profile' },
      ...(credit ? [{ id: 'credit', label: 'Credit' }] : []),
      { id: 'activity', label: 'Activity' },
      { id: 'security', label: 'Security' },
      { id: 'notifications', label: 'Notifications' },
      { id: 'mcp', label: 'MCP keys' },
    ]}
    bind:active={tab}
  />

  {#if error}<Alert type="danger">{error}</Alert>{/if}
  {#if success}<Alert type="success">{success}</Alert>{/if}

  {#if tab === 'mcp'}
    <ClientMcpKeys {impersonating} />
  {:else if tab === 'notifications'}
    <ClientNotifications {impersonating} />
  {:else if loading}
    <div class="state"><Spinner /><span>Loading…</span></div>
  {:else if tab === 'credit' && credit}
    <ClientCreditPanel {credit} {impersonating} on:changed={refreshCredit} />
  {:else if tab === 'profile'}
    <div class="panel">
      <h2>Billing profile</h2>
      <div class="grid">
        <label>Legal name <input bind:value={profile.legal_name} /></label>
        <label>Company <input bind:value={profile.company} /></label>
        <label>Invoice email <input type="email" bind:value={profile.invoice_email} /></label>
        <label>Phone <input bind:value={profile.phone} /></label>
        <label>Address <input bind:value={profile.address_line1} /></label>
        <label>City <input bind:value={profile.city} /></label>
        <label>Region <input bind:value={profile.region} /></label>
        <label>Postal code <input bind:value={profile.postal_code} /></label>
        <label>Country (2-letter) <input bind:value={profile.country} maxlength="2" /></label>
      </div>

      <h3>VAT and tax</h3>
      <label class="check">
        <input type="checkbox" bind:checked={profile.is_business} />
        I am buying as a business
      </label>
      <div class="grid">
        <label>Type of tax number
          <select bind:value={profile.tax_id_type}>
            <option value={null}>Work it out from the number</option>
            {#each TAX_ID_TYPE_OPTIONS as option (option.value)}
              <option value={option.value}>{option.label}</option>
            {/each}
          </select>
        </label>
        <label>Tax number <input bind:value={profile.tax_id} autocomplete="off" /></label>
      </div>
      <div class="tax-status" aria-live="polite">
        {#if checking}
          <Spinner />
          <span>Checking your VAT number with the tax authority. This can take a few seconds…</span>
        {:else}
          {#if taxChanged && profile.tax_id}
            <Badge variant="muted" caps={false}>Not checked</Badge>
            <span>Your number will be checked with the tax authority when you save.</span>
          {:else}
            <Badge variant={taxStatus.variant} caps={false}>{taxStatus.label}</Badge>
            <span>
              {taxStatus.text}
              {#if profile.tax_id_checked_at && profile.tax_id}
                Last checked {new Date(profile.tax_id_checked_at).toLocaleString()}.
              {/if}
            </span>
          {/if}
        {/if}
      </div>
      <Button on:click={saveProfile} disabled={working}>{checking ? 'Saving and checking…' : 'Save profile'}</Button>

      <h3>Email history</h3>
      {#if emails.length === 0}
        <p class="muted">No emails logged yet.</p>
      {:else}
        <ul class="simple-list">
          {#each emails as row (row.id)}
            <li><span>{row.subject}</span><small>{row.status} · {new Date(row.created_at).toLocaleString()}</small></li>
          {/each}
        </ul>
      {/if}
    </div>
  {:else if tab === 'activity'}
    <div class="panel">
      <h2>Recent activity</h2>
      {#if activity.length === 0}
        <p class="muted">No activity recorded yet.</p>
      {:else}
        <ul class="simple-list">
          {#each activity as row (row.id)}
            <li>
              <span><code>{row.action}</code> {row.resource_type}{row.resource_id ? ` #${row.resource_id}` : ''}</span>
              <small>{new Date(row.created_at).toLocaleString()}</small>
            </li>
          {/each}
        </ul>
      {/if}
    </div>
  {:else}
    <div class="panel">
      <h2>Discord</h2>
      <p class="muted">Link your Discord account for onboarding requirements on certain products.</p>
      <div class="actions">
        <Button on:click={linkDiscord} disabled={working || impersonating}>Link Discord</Button>
        <Button variant="secondary" on:click={unlinkDiscord} disabled={working || impersonating}>Unlink</Button>
      </div>

      <h2>Two-factor authentication</h2>
      {#if impersonating}
        <p class="muted">Two-factor settings can't be changed while signed in on the client's behalf.</p>
      {:else}
        <TwoFactorPanel />
      {/if}
    </div>
  {/if}
</section>


<style>
  .page-head {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 16px;
    margin-bottom: 20px;
  }
  .page-head h1 {
    margin: 0;
    font-size: 26px;
    font-weight: 750;
    letter-spacing: -0.02em;
  }
  .sub {
    margin: 4px 0 0;
    font-size: 14px;
    color: var(--text-tertiary);
  }
  .state { min-height: 200px; display: grid; place-items: center; gap: 12px; margin-top: 16px; }
  .panel { margin-top: 16px; border: 1px solid var(--border-color); border-radius: var(--radius-lg); background: var(--portal-card-bg, var(--bg-primary)); padding: 20px; box-shadow: var(--shadow-sm); }
  h2 { margin: 0 0 12px; font-size: 17px; }
  h3 { margin: 24px 0 10px; font-size: 15px; }
  .grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 12px; margin-bottom: 16px; }
  label { display: grid; gap: 6px; font-size: 13px; font-weight: 600; }
  select { padding: 9px 10px; border: 1px solid var(--border-color); border-radius: 8px; background: var(--bg-primary); color: var(--text-primary); }
  .check { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; font-weight: 500; }
  .tax-status { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 16px; font-size: 13px; color: var(--text-secondary); }
  input { padding: 9px 10px; border: 1px solid var(--border-color); border-radius: 8px; background: var(--bg-primary); color: var(--text-primary); }
  .actions { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 20px; }
  .simple-list { list-style: none; margin: 0; padding: 0; }
  .simple-list li { display: flex; justify-content: space-between; gap: 12px; padding: 10px 0; border-bottom: 1px solid var(--border-color); font-size: 14px; }
  .simple-list small { color: var(--text-tertiary); white-space: nowrap; }
  .muted { color: var(--text-secondary); font-size: 14px; }
  code { font-size: 12px; }
  @media (max-width: 720px) { .grid { grid-template-columns: 1fr; } }
</style>
