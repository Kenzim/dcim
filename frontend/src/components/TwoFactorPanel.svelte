<script>
  import { createEventDispatcher, onMount } from 'svelte';
  import {
    getTwoFactorStatus,
    twoFactorSetup,
    twoFactorConfirm,
    twoFactorRegenerateRecoveryCodes,
    twoFactorDisable,
  } from '../lib/api.js';
  import { Alert, Button } from './ui/index.js';

  // When true the account must enrol before using the app (admin-mandated 2FA).
  export let enrollmentRequired = false;

  const dispatch = createEventDispatcher();

  let status = null;
  let loading = true;
  let error = '';
  let message = '';
  let busy = false;

  // Which form is open: setup | regenerate | disable | null
  let mode = null;
  let password = '';
  let code = '';

  // Set after enrolment or regeneration: the one-time recovery codes to save.
  let setup = null;
  let recoveryCodes = null;

  async function refresh() {
    status = await getTwoFactorStatus();
  }

  onMount(async () => {
    try {
      await refresh();
    } catch (e) {
      error = e.message;
    } finally {
      loading = false;
    }
  });

  function resetForms() {
    mode = null;
    password = '';
    code = '';
    error = '';
  }

  async function run(action) {
    error = '';
    message = '';
    busy = true;
    try {
      await action();
    } catch (e) {
      error = e.message || 'Something went wrong';
    } finally {
      busy = false;
    }
  }

  const startSetup = () => run(async () => {
    setup = await twoFactorSetup(password);
    mode = 'confirm';
    password = '';
  });

  const confirmSetup = () => run(async () => {
    const result = await twoFactorConfirm(code.trim());
    recoveryCodes = result.recovery_codes;
    setup = null;
    resetForms();
    await refresh();
    message = 'Two-factor authentication is on.';
  });

  const regenerate = () => run(async () => {
    const result = await twoFactorRegenerateRecoveryCodes(code.trim());
    recoveryCodes = result.recovery_codes;
    resetForms();
    await refresh();
    message = 'New recovery codes generated. Older codes no longer work.';
  });

  const disable = () => run(async () => {
    await twoFactorDisable(code.trim());
    resetForms();
    await refresh();
    message = 'Two-factor authentication is off.';
  });

  function dismissRecoveryCodes() {
    recoveryCodes = null;
    if (enrollmentRequired) {
      dispatch('enrolled');
    }
  }

  async function copyRecoveryCodes() {
    try {
      await navigator.clipboard.writeText(recoveryCodes.join('\n'));
      message = 'Recovery codes copied.';
    } catch (_) {
      error = 'Could not copy. Select the codes and copy them manually.';
    }
  }
</script>

<div class="twofactor">
  {#if enrollmentRequired && !status?.enabled}
    <Alert type="warning">
      Your administrator requires two-factor authentication for this account. Set it up below to continue.
    </Alert>
  {/if}

  {#if error}<Alert type="danger">{error}</Alert>{/if}
  {#if message}<Alert type="success">{message}</Alert>{/if}

  {#if loading}
    <p class="muted">Loading…</p>
  {:else if recoveryCodes}
    <div class="recovery-box">
      <h4>Save your recovery codes</h4>
      <p class="hint">
        Each code works once if you lose your authenticator. They are shown only now.
        Store them somewhere safe.
      </p>
      <ul class="recovery-codes">
        {#each recoveryCodes as recoveryCode}
          <li><code>{recoveryCode}</code></li>
        {/each}
      </ul>
      <div class="row">
        <Button variant="secondary" on:click={copyRecoveryCodes}>Copy codes</Button>
        <Button on:click={dismissRecoveryCodes}>I've saved them</Button>
      </div>
    </div>
  {:else if status?.enabled}
    <p>
      Two-factor authentication is <strong>on</strong>.
      {status.recovery_codes_remaining} recovery code{status.recovery_codes_remaining === 1 ? '' : 's'} left.
    </p>

    {#if mode === 'regenerate'}
      <div class="form">
        <label>Authenticator or recovery code
          <input bind:value={code} inputmode="text" autocomplete="one-time-code" maxlength="64" />
        </label>
        <div class="row">
          <Button on:click={regenerate} disabled={busy || !code.trim()}>Generate new codes</Button>
          <Button variant="secondary" on:click={resetForms} disabled={busy}>Cancel</Button>
        </div>
      </div>
    {:else if mode === 'disable'}
      <div class="form">
        <label>Authenticator or recovery code
          <input bind:value={code} inputmode="text" autocomplete="one-time-code" maxlength="64" />
        </label>
        <div class="row">
          <Button variant="danger" on:click={disable} disabled={busy || !code.trim()}>Turn off 2FA</Button>
          <Button variant="secondary" on:click={resetForms} disabled={busy}>Cancel</Button>
        </div>
      </div>
    {:else}
      <div class="row">
        <Button variant="secondary" on:click={() => { resetForms(); mode = 'regenerate'; }}>New recovery codes</Button>
        <Button variant="danger" on:click={() => { resetForms(); mode = 'disable'; }}>Turn off 2FA</Button>
      </div>
    {/if}
  {:else if mode === 'confirm' && setup}
    <div class="form">
      <p>Add this account to your authenticator app using the key below, then enter the 6-digit code it shows.</p>
      <p><code class="secret">{setup.secret}</code></p>
      <p class="hint"><a href={setup.otpauth_uri}>Open in authenticator app</a></p>
      <label>Verification code
        <input bind:value={code} inputmode="numeric" autocomplete="one-time-code" maxlength="8" />
      </label>
      <div class="row">
        <Button on:click={confirmSetup} disabled={busy || code.trim().length < 6}>Turn on 2FA</Button>
        <Button variant="secondary" on:click={resetForms} disabled={busy}>Cancel</Button>
      </div>
    </div>
  {:else if mode === 'setup'}
    <div class="form">
      <label>Current password
        <input type="password" bind:value={password} autocomplete="current-password" />
      </label>
      <div class="row">
        <Button on:click={startSetup} disabled={busy || !password}>Continue</Button>
        <Button variant="secondary" on:click={resetForms} disabled={busy}>Cancel</Button>
      </div>
    </div>
  {:else}
    <p>
      Two-factor authentication is <strong>off</strong>. Turn it on to require a code from an
      authenticator app when you sign in.
    </p>
    <Button on:click={() => { resetForms(); mode = 'setup'; }}>Set up two-factor authentication</Button>
  {/if}
</div>

<style>
  .twofactor {
    display: flex;
    flex-direction: column;
    gap: 14px;
  }
  .form {
    display: flex;
    flex-direction: column;
    gap: 10px;
    max-width: 420px;
  }
  .form label {
    display: flex;
    flex-direction: column;
    gap: 6px;
    font-weight: 500;
  }
  .row {
    display: flex;
    gap: 10px;
    flex-wrap: wrap;
  }
  .hint,
  .muted {
    color: var(--text-secondary);
    font-size: 13px;
    margin: 0;
  }
  .secret {
    word-break: break-all;
    user-select: all;
  }
  .recovery-box {
    border: 1px solid var(--border-color);
    border-radius: 10px;
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .recovery-box h4 {
    margin: 0;
  }
  .recovery-codes {
    list-style: none;
    padding: 0;
    margin: 0;
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(190px, 1fr));
    gap: 8px;
  }
  .recovery-codes code {
    font-size: 14px;
    letter-spacing: 0.5px;
  }
</style>
