<script>
  import { confirmPasswordReset } from '../lib/api.js';
  import { Alert, Button } from './ui/index.js';

  // The emailed link is /reset-password?token=...
  const token = new URLSearchParams(window.location.search).get('token') || '';

  let password = '';
  let confirmPassword = '';
  let busy = false;
  let error = '';
  let done = false;

  const byteLength = (value) => new TextEncoder().encode(value).length;

  async function submit() {
    error = '';
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
      await confirmPasswordReset(token, password);
      done = true;
    } catch (e) {
      error = e.message || 'Could not reset password';
    } finally {
      busy = false;
    }
  }
</script>

<div class="auth-page">
  <main class="auth-card">
    <h1>Choose a new password</h1>
    {#if !token}
      <Alert type="danger">This reset link is incomplete. Request a new one.</Alert>
      <a href="/forgot-password" class="auth-link">Request a new link</a>
    {:else if done}
      <Alert type="success">Your password has been changed. All other sessions were signed out.</Alert>
      <a href="/login" class="auth-link">Sign in</a>
    {:else}
      {#if error}<Alert type="danger">{error}</Alert>{/if}
      <form on:submit|preventDefault={submit} class="auth-form">
        <label>New password
          <input type="password" bind:value={password} required minlength="8" autocomplete="new-password" disabled={busy} />
        </label>
        <label>Confirm new password
          <input type="password" bind:value={confirmPassword} required minlength="8" autocomplete="new-password" disabled={busy} />
        </label>
        <Button type="submit" disabled={busy}>{busy ? 'Saving…' : 'Set new password'}</Button>
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
  .auth-form label {
    display: flex;
    flex-direction: column;
    gap: 6px;
    font-weight: 500;
  }
  .auth-link {
    color: var(--accent-color);
    font-size: 14px;
  }
</style>
