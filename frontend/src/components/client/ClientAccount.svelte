<script>
  import { onMount } from 'svelte';
  import { Alert, Button, Modal, Spinner, Tabs } from '../ui/index.js';
  import {
    clientCommerce2faConfirm,
    clientCommerce2faDisable,
    clientCommerce2faSetup,
    clientCommerceDiscordAuthorizeUrl,
    clientCommerceDiscordUnlink,
    clientCommerceGetProfile,
    clientCommerceListActivity,
    clientCommerceListEmails,
    clientCommerceUpdateProfile,
  } from '../../lib/api.js';

  export let impersonating = false;

  let tab = 'profile';
  let profile = {};
  let activity = [];
  let emails = [];
  let loading = true;
  let working = false;
  let error = '';
  let success = '';
  let totpSetup = null;
  let totpCode = '';
  let disableModal = { open: false, code: '' };

  onMount(load);

  async function load() {
    loading = true;
    error = '';
    try {
      [profile, activity, emails] = await Promise.all([
        clientCommerceGetProfile().catch(() => ({})),
        clientCommerceListActivity().catch(() => []),
        clientCommerceListEmails().catch(() => []),
      ]);
    } catch (err) {
      error = err.message || 'Failed to load account data';
    } finally {
      loading = false;
    }
  }

  async function saveProfile() {
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
        tax_id: profile.tax_id || null,
        invoice_email: profile.invoice_email || null,
      });
      success = 'Profile saved.';
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

  async function start2fa() {
    working = true;
    error = '';
    try {
      totpSetup = await clientCommerce2faSetup();
      totpCode = '';
    } catch (err) {
      error = err.message || '2FA setup failed';
    } finally {
      working = false;
    }
  }

  async function confirm2fa() {
    working = true;
    error = '';
    try {
      await clientCommerce2faConfirm(totpCode.trim());
      totpSetup = null;
      totpCode = '';
      success = 'Two-factor authentication enabled.';
    } catch (err) {
      error = err.message || 'Invalid code';
    } finally {
      working = false;
    }
  }

  async function disable2fa() {
    working = true;
    error = '';
    try {
      await clientCommerce2faDisable(disableModal.code.trim());
      disableModal = { open: false, code: '' };
      success = 'Two-factor authentication disabled.';
    } catch (err) {
      error = err.message || 'Failed to disable 2FA';
    } finally {
      working = false;
    }
  }
</script>

<section aria-labelledby="account-title">
  <div class="page-head">
    <div>
      <h1 id="account-title">Account</h1>
      <p class="sub">Billing profile, security, and activity.</p>
    </div>
  </div>

  <Tabs
    tabs={[
      { id: 'profile', label: 'Profile' },
      { id: 'activity', label: 'Activity' },
      { id: 'security', label: 'Security' },
    ]}
    bind:active={tab}
  />

  {#if error}<Alert type="danger">{error}</Alert>{/if}
  {#if success}<Alert type="success">{success}</Alert>{/if}

  {#if loading}
    <div class="state"><Spinner /><span>Loading…</span></div>
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
        <label>Tax ID <input bind:value={profile.tax_id} /></label>
      </div>
      <Button on:click={saveProfile} disabled={working}>Save profile</Button>

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
      {#if totpSetup}
        <p>Scan this secret in your authenticator app: <code>{totpSetup.secret}</code></p>
        <p class="muted"><a href={totpSetup.otpauth_uri}>Open in authenticator</a></p>
        <label>Verification code <input bind:value={totpCode} inputmode="numeric" maxlength="8" /></label>
        <Button on:click={confirm2fa} disabled={working || totpCode.length < 6}>Confirm 2FA</Button>
      {:else}
        <Button on:click={start2fa} disabled={working}>Set up 2FA</Button>
        <Button variant="secondary" on:click={() => (disableModal = { open: true, code: '' })} disabled={working}>Disable 2FA</Button>
      {/if}
    </div>
  {/if}
</section>

{#if disableModal.open}
  <Modal title="Disable 2FA" onClose={() => (disableModal = { open: false, code: '' })}>
    <label>Enter your current TOTP code <input bind:value={disableModal.code} inputmode="numeric" maxlength="8" /></label>
    <svelte:fragment slot="footer">
      <Button variant="secondary" on:click={() => (disableModal = { open: false, code: '' })}>Cancel</Button>
      <Button variant="danger" on:click={disable2fa} disabled={working}>Disable</Button>
    </svelte:fragment>
  </Modal>
{/if}

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
  input { padding: 9px 10px; border: 1px solid var(--border-color); border-radius: 8px; background: var(--bg-primary); color: var(--text-primary); }
  .actions { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 20px; }
  .simple-list { list-style: none; margin: 0; padding: 0; }
  .simple-list li { display: flex; justify-content: space-between; gap: 12px; padding: 10px 0; border-bottom: 1px solid var(--border-color); font-size: 14px; }
  .simple-list small { color: var(--text-tertiary); white-space: nowrap; }
  .muted { color: var(--text-secondary); font-size: 14px; }
  code { font-size: 12px; }
  @media (max-width: 720px) { .grid { grid-template-columns: 1fr; } }
</style>
