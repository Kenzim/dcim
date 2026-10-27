<script>
  import { onMount } from 'svelte';
  import { getCurrentUser, getTwoFactorStatus, logout as apiLogout } from '../lib/api.js';
  import { navigate } from '../lib/router.js';
  import { user, isAuthenticated } from '../stores/auth.js';
  import { Alert, Button, Spinner } from './ui/index.js';
  import TwoFactorPanel from './TwoFactorPanel.svelte';

  let loading = true;
  let error = '';
  let enrollmentRequired = false;
  let me = null;

  onMount(async () => {
    try {
      const [current, twoFactor] = await Promise.all([getCurrentUser(), getTwoFactorStatus()]);
      if (!current) {
        navigate('/login');
        return;
      }
      me = current;
      enrollmentRequired = twoFactor.enrollment_required;
    } catch (e) {
      error = e.message || 'Could not load account security';
    } finally {
      loading = false;
    }
  });

  function homeFor(account) {
    if (account?.is_admin) return '/admin';
    if (account?.is_reseller) return '/reseller';
    return '/client';
  }

  function onEnrolled() {
    enrollmentRequired = false;
    navigate(homeFor(me));
  }

  async function signOut() {
    try {
      await apiLogout();
    } catch (_) {
      // Session may already be gone; clear local state regardless.
    }
    user.set(null);
    isAuthenticated.set(false);
    navigate('/login');
  }
</script>

<div class="security-page">
  <header class="security-header">
    <strong>Rackflow</strong>
    <div class="header-actions">
      {#if !enrollmentRequired && me}
        <Button variant="secondary" size="small" on:click={() => navigate(homeFor(me))}>Back to app</Button>
      {/if}
      <Button variant="secondary" size="small" on:click={signOut}>Sign out</Button>
    </div>
  </header>

  <main class="security-card">
    <h1>Account security</h1>
    {#if loading}
      <Spinner size="small" />
    {:else if error}
      <Alert type="danger">{error}</Alert>
    {:else}
      <p class="account-line">Signed in as <strong>{me?.username}</strong></p>
      <TwoFactorPanel {enrollmentRequired} on:enrolled={onEnrolled} />
    {/if}
  </main>
</div>

<style>
  .security-page {
    min-height: 100vh;
    background: var(--bg-secondary);
    padding: 16px;
  }
  .security-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    max-width: 720px;
    margin: 0 auto 16px;
  }
  .header-actions {
    display: flex;
    gap: 8px;
  }
  .security-card {
    max-width: 720px;
    margin: 0 auto;
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: 16px;
    padding: 28px;
    display: flex;
    flex-direction: column;
    gap: 16px;
  }
  .security-card h1 {
    font-size: 22px;
    margin: 0;
  }
  .account-line {
    margin: 0;
    color: var(--text-secondary);
  }
</style>
