<script>
  import { onMount } from 'svelte';
  import { user } from '../stores/auth.js';
  import { getCurrentUser, getSessions, deleteSession } from '../lib/api.js';
  import PageHeader from './PageHeader.svelte';

  let userData = null;
  let sessions = [];
  let loading = true;
  let sessionsLoading = false;
  let error = null;

  onMount(async () => {
    await loadUserData();
    await loadSessions();
  });

  async function loadUserData() {
    loading = true;
    error = null;
    try {
      userData = await getCurrentUser();
      if (userData) {
        user.set(userData);
      }
    } catch (err) {
      error = err.message || 'Failed to load user data';
      console.error('Error loading user data:', err);
    } finally {
      loading = false;
    }
  }

  async function loadSessions() {
    sessionsLoading = true;
    try {
      sessions = await getSessions() || [];
    } catch (err) {
      console.error('Error loading sessions:', err);
      sessions = [];
    } finally {
      sessionsLoading = false;
    }
  }

  async function handleDeleteSession(tokenId) {
    if (!confirm('Are you sure you want to delete this session?')) {
      return;
    }

    try {
      await deleteSession(tokenId);
      // Reload sessions after deletion
      await loadSessions();
    } catch (err) {
      alert(err.message || 'Failed to delete session');
      console.error('Error deleting session:', err);
    }
  }

  function formatDate(dateString) {
    if (!dateString || dateString === 'unknown') return 'Unknown';
    try {
      const date = new Date(dateString);
      return date.toLocaleString();
    } catch {
      return dateString;
    }
  }

</script>

<div class="user-page">
  <PageHeader title="User Profile" />

  <div class="page-content">
    {#if loading}
      <div class="loading-container">
        <div class="spinner"></div>
        <p>Loading user data...</p>
      </div>
    {:else if error}
      <div class="alert alert-error">
        <svg xmlns="http://www.w3.org/2000/svg" class="alert-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
        </svg>
        {error}
      </div>
    {:else if userData}
      <div class="cards-grid">
        <div class="info-card">
          <div class="card-icon user-icon">
            <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
            </svg>
          </div>
          <div class="card-content">
            <h3 class="card-label">User ID</h3>
            <p class="card-value">#{userData.id}</p>
          </div>
        </div>

        <div class="info-card">
          <div class="card-icon username-icon">
            <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
            </svg>
          </div>
          <div class="card-content">
            <h3 class="card-label">Username</h3>
            <p class="card-value">{userData.username}</p>
          </div>
        </div>

        <div class="info-card">
          <div class="card-icon email-icon">
            <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 8l7.89 5.26a2 2 0 002.22 0L21 8M5 19h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" />
            </svg>
          </div>
          <div class="card-content">
            <h3 class="card-label">Email</h3>
            <p class="card-value">{userData.email}</p>
          </div>
        </div>

        <div class="info-card">
          <div class="card-icon admin-icon">
            <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" />
            </svg>
          </div>
          <div class="card-content">
            <h3 class="card-label">Admin Status</h3>
            <p class="card-value">
              {#if userData.is_admin}
                <span class="badge badge-success">Administrator</span>
              {:else}
                <span class="badge badge-secondary">User</span>
              {/if}
            </p>
          </div>
        </div>
      </div>

      <div class="sessions-card">
        <div class="card-header">
          <h2>Active Sessions</h2>
          <p class="card-subtitle">Manage your active login sessions</p>
        </div>
        <div class="card-body">
          {#if sessionsLoading}
            <div class="sessions-loading">
              <div class="spinner-small"></div>
              <span>Loading sessions...</span>
            </div>
          {:else if sessions.length === 0}
            <div class="sessions-empty">No active sessions</div>
          {:else}
            <div class="sessions-list">
              {#each sessions as session}
                <div class="session-item" class:current={session.is_current}>
                  <div class="session-header">
                    <span class="session-token">{session.token}</span>
                    {#if session.is_current}
                      <span class="session-badge">Current Session</span>
                    {/if}
                  </div>
                  <div class="session-details">
                    <div class="session-detail">
                      <span class="detail-label">IP Address:</span>
                      <span class="detail-value">{session.last_seen_ip}</span>
                    </div>
                    <div class="session-detail">
                      <span class="detail-label">Last Seen:</span>
                      <span class="detail-value">{formatDate(session.last_seen_at)}</span>
                    </div>
                    <div class="session-detail">
                      <span class="detail-label">Created:</span>
                      <span class="detail-value">{formatDate(session.created_at)}</span>
                    </div>
                  </div>
                </div>
              {/each}
            </div>
          {/if}
        </div>
      </div>
    {/if}
  </div>
</div>

<style>
  .user-page {
    min-height: 100vh;
    background: #f8fafc;
  }


  .page-content {
    padding: 32px;
  }

  .loading-container {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 80px 20px;
    color: var(--text-secondary);
  }

  .spinner {
    width: 48px;
    height: 48px;
    border: 4px solid var(--border-color);
    border-top-color: var(--primary-color);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
    margin-bottom: 16px;
  }

  @keyframes spin {
    to { transform: rotate(360deg); }
  }

  .alert {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 16px 20px;
    border-radius: 12px;
    margin-bottom: 24px;
  }

  .alert-error {
    background: #fee2e2;
    color: #991b1b;
    border: 1px solid #fecaca;
  }

  .alert-icon {
    width: 20px;
    height: 20px;
    flex-shrink: 0;
  }

  .cards-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
    gap: 24px;
    margin-bottom: 32px;
  }

  .info-card {
    background: white;
    border-radius: 16px;
    padding: 24px;
    display: flex;
    align-items: center;
    gap: 20px;
    box-shadow: var(--shadow-md);
    transition: all 0.3s ease;
    border: 1px solid var(--border-color);
  }

  .info-card:hover {
    transform: translateY(-4px);
    box-shadow: var(--shadow-lg);
  }

  .card-icon {
    width: 56px;
    height: 56px;
    border-radius: 12px;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
  }

  .card-icon svg {
    width: 28px;
    height: 28px;
    color: white;
  }

  .user-icon {
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  }

  .username-icon {
    background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);
  }

  .email-icon {
    background: linear-gradient(135deg, #4facfe 0%, #00f2fe 100%);
  }

  .admin-icon {
    background: linear-gradient(135deg, #43e97b 0%, #38f9d7 100%);
  }

  .card-content {
    flex: 1;
  }

  .card-label {
    font-size: 12px;
    font-weight: 600;
    color: var(--text-secondary);
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin: 0 0 8px;
  }

  .card-value {
    font-size: 20px;
    font-weight: 700;
    color: var(--text-primary);
    margin: 0;
  }

  .badge {
    display: inline-block;
    padding: 6px 12px;
    border-radius: 6px;
    font-size: 12px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }

  .badge-success {
    background: #d1fae5;
    color: #065f46;
  }

  .badge-secondary {
    background: #e2e8f0;
    color: #475569;
  }

  .sessions-card {
    background: white;
    border-radius: 16px;
    box-shadow: var(--shadow-md);
    border: 1px solid var(--border-color);
    overflow: hidden;
  }

  .card-header {
    padding: 24px 32px;
    border-bottom: 1px solid var(--border-color);
    background: linear-gradient(135deg, #f8fafc 0%, #ffffff 100%);
  }

  .card-header h2 {
    font-size: 20px;
    font-weight: 700;
    margin: 0 0 4px;
    color: var(--text-primary);
  }

  .card-subtitle {
    font-size: 14px;
    color: var(--text-secondary);
    margin: 0;
  }

  .card-body {
    padding: 32px;
  }

  .sessions-loading {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 12px;
    padding: 40px;
    color: var(--text-secondary);
  }

  .spinner-small {
    width: 24px;
    height: 24px;
    border: 3px solid var(--border-color);
    border-top-color: var(--primary-color);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }

  .sessions-empty {
    text-align: center;
    padding: 40px;
    color: var(--text-secondary);
  }

  .sessions-list {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .session-item {
    background: #f8fafc;
    border: 1px solid var(--border-color);
    border-radius: 12px;
    padding: 20px;
    transition: all 0.2s ease;
  }

  .session-item:hover {
    border-color: var(--primary-color);
    box-shadow: var(--shadow-sm);
  }

  .session-item.current {
    background: linear-gradient(135deg, rgba(102, 126, 234, 0.05) 0%, rgba(118, 75, 162, 0.05) 100%);
    border-color: var(--primary-color);
    box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
  }

  .session-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 16px;
  }

  .session-header-right {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .delete-session-btn {
    background: transparent;
    border: none;
    padding: 4px;
    cursor: pointer;
    color: #ef4444;
    border-radius: 4px;
    transition: all 0.2s ease;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .delete-session-btn:hover {
    background: #fee2e2;
    color: #dc2626;
  }

  .delete-icon {
    width: 18px;
    height: 18px;
  }

  .session-token {
    font-family: 'Courier New', monospace;
    font-size: 14px;
    color: var(--text-primary);
    font-weight: 600;
  }

  .session-badge {
    padding: 4px 10px;
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    color: white;
    border-radius: 6px;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }

  .session-details {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 12px;
  }

  .session-detail {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .detail-label {
    font-size: 11px;
    font-weight: 600;
    color: var(--text-secondary);
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }

  .detail-value {
    font-size: 14px;
    color: var(--text-primary);
    font-weight: 500;
  }
</style>

