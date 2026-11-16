<script>
  import { requestPasswordReset } from '../lib/api.js';
  import { Alert, Button } from './ui/index.js';

  let email = '';
  let busy = false;
  let error = '';
  let sent = false;

  async function submit() {
    error = '';
    busy = true;
    try {
      await requestPasswordReset(email.trim());
      sent = true;
    } catch (e) {
      error = e.message || 'Could not send reset email';
    } finally {
      busy = false;
    }
  }
</script>

<div class="auth-page">
  <main class="auth-card">
    <h1>Reset your password</h1>
    {#if sent}
      <Alert type="success">
        If an account exists for that address, a reset link is on its way. The link expires in 2 hours.
      </Alert>
      <a href="/login" class="auth-link">Back to sign in</a>
    {:else}
      {#if error}<Alert type="danger">{error}</Alert>{/if}
      <p class="hint">Enter the email address on your account and we'll send you a link to choose a new password.</p>
      <form on:submit|preventDefault={submit} class="auth-form">
        <div class="form-group">
          <label for="reset-email">Email</label>
          <input id="reset-email" type="email" bind:value={email} required autocomplete="email" placeholder="you@example.com" disabled={busy} />
        </div>
        <Button type="submit" disabled={busy || !email.trim()}>{busy ? 'Sending…' : 'Send reset link'}</Button>
      </form>
      <a href="/login" class="auth-link">Back to sign in</a>
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
  .hint {
    margin: 0;
    color: var(--text-secondary);
    font-size: 14px;
  }
  .auth-link {
    color: var(--accent-color);
    font-size: 14px;
  }
</style>
