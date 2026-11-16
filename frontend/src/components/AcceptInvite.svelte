<script>
  import { onMount } from 'svelte';
  import { lookupInvite, acceptInvite } from '../lib/api.js';
  import { Alert, Button } from './ui/index.js';

  // The emailed link is /accept-invite?token=...
  const token = new URLSearchParams(window.location.search).get('token') || '';

  let loading = !!token;
  let invalid = !token;
  let email = '';
  let kind = '';
  let username = '';
  let password = '';
  let confirmPassword = '';
  let busy = false;
  let error = '';
  let done = false;

  const byteLength = (value) => new TextEncoder().encode(value).length;

  onMount(async () => {
    if (!token) return;
    try {
      const info = await lookupInvite(token);
      email = info.email;
      kind = info.kind;
      username = info.username || '';
    } catch (e) {
      invalid = true;
    } finally {
      loading = false;
    }
  });

  async function submit() {
    error = '';
    const name = username.trim();
    if (name.length < 3) {
      error = 'Username must be at least 3 characters';
      return;
    }
    if (/\s/.test(name)) {
      error = 'Username cannot contain spaces';
      return;
    }
    if (password.length < 8) {
      error = 'Password must be at least 8 characters';
      return;
    }
    if (byteLength(password) > 72) {
      error = 'Password must be 72 bytes or fewer';
      return;
    }
    if (password !== confirmPassword) {
      error = 'Passwords do not match';
      return;
    }
    busy = true;
    try {
      await acceptInvite(token, name, password);
      done = true;
    } catch (e) {
      error = e.message || 'Could not accept the invitation';
    } finally {
      busy = false;
    }
  }
</script>

<div class="auth-page">
  <main class="auth-card">
    <h1>Accept your invitation</h1>
    {#if loading}
      <p>Checking your invitation…</p>
    {:else if invalid}
      <Alert type="danger">This invitation link is invalid or has expired. Ask for a new one.</Alert>
      <a href="/login" class="auth-link">Go to sign in</a>
    {:else if done}
      <Alert type="success">Your account is ready. You can now sign in.</Alert>
      <a href="/login" class="auth-link">Sign in</a>
    {:else}
      <p class="intro">
        Choose a username and password for {email}{kind === 'staff' ? ' (staff account)' : ''}.
      </p>
      {#if error}<Alert type="danger">{error}</Alert>{/if}
      <form on:submit|preventDefault={submit} class="auth-form">
        <div class="form-group">
          <label for="invite-username">Username</label>
          <input id="invite-username" type="text" bind:value={username} required minlength="3" autocomplete="username" disabled={busy} />
        </div>
        <div class="form-group">
          <label for="new-password">Password</label>
          <input id="new-password" type="password" bind:value={password} required minlength="8" autocomplete="new-password" placeholder="At least 8 characters" disabled={busy} />
        </div>
        <div class="form-group">
          <label for="confirm-password">Confirm password</label>
          <input id="confirm-password" type="password" bind:value={confirmPassword} required minlength="8" autocomplete="new-password" disabled={busy} />
        </div>
        <Button type="submit" disabled={busy}>{busy ? 'Saving…' : 'Accept invitation'}</Button>
      </form>
    {/if}
  </main>
</div>

<style>
  .auth-page {
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 20px;
    background: var(--bg-secondary);
  }
  .auth-card {
    width: 100%;
    max-width: 420px;
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: 16px;
    padding: 32px 28px;
    display: flex;
    flex-direction: column;
    gap: 16px;
  }
  .auth-card h1 {
    font-size: 22px;
    margin: 0;
  }
  .auth-form {
    display: flex;
    flex-direction: column;
    gap: 14px;
  }
  .auth-form .form-group {
    margin-bottom: 0;
  }
  .auth-form .form-group input {
    padding: 12px 14px;
    font-size: 15px;
  }
  .auth-form :global(button[type='submit']) {
    width: 100%;
    justify-content: center;
    margin-top: 4px;
  }
  .auth-link {
    color: var(--accent-color);
    font-size: 14px;
  }
  .intro {
    margin: 0;
    font-size: 14px;
    color: var(--text-secondary);
  }
</style>
