<script>
  import { login } from '../lib/api.js';
  import { user, isAuthenticated } from '../stores/auth.js';
  import { navigate } from '../lib/router.js';
  import { theme, toggleTheme } from '../stores/theme.js';
  import { Alert, FormGroup, Spinner } from './ui/index.js';

  let username = '';
  let password = '';
  let error = '';
  let loading = false;

  async function handleLogin() {
    error = '';
    loading = true;

    try {
      const result = await login(username, password);
      
      // Cookie is set automatically by the server
      // Update store directly from login response
      user.set({
        id: result.user_id,
        username: result.username,
        email: '', // Will be fetched from /me endpoint when needed
        is_admin: result.is_admin
      });
      isAuthenticated.set(true);
      loading = false;
      // Redirect to admin panel
      navigate('/admin');
    } catch (err) {
      error = err.message || 'Login failed';
      console.error('Login error:', err);
      loading = false;
    }
  }

  function handleKeyPress(event) {
    if (event.key === 'Enter') {
      handleLogin();
    }
  }
</script>

<div class="login-container">
  <button class="theme-toggle-login" on:click={toggleTheme} title="Toggle theme" aria-label="Toggle theme">
    {#if $theme === 'light'}
      <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20.354 15.354A9 9 0 018.646 3.646 9.003 9.003 0 0012 21a9.003 9.003 0 008.354-5.646z" /></svg>
    {:else}
      <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 3v1m0 16v1m9-9h-1M4 12H3m15.364 6.364l-.707-.707M6.343 6.343l-.707-.707m12.728 0l-.707.707M6.343 17.657l-.707.707M16 12a4 4 0 11-8 0 4 4 0 018 0z" /></svg>
    {/if}
  </button>
  <div class="login-wrapper">
    <div class="login-card">
      <div class="login-header">
        <div class="logo-icon">
          <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" />
          </svg>
        </div>
        <h1 class="login-title">Rackflow</h1>
        <p class="login-subtitle">Data Center Infrastructure Management</p>
      </div>
      
      <div class="login-body">
        {#if error}
          <Alert type="danger">{error}</Alert>
        {/if}

        <form on:submit|preventDefault={handleLogin} class="login-form">
          <div class="form-group">
            <label for="username" class="form-label">
              <svg xmlns="http://www.w3.org/2000/svg" class="input-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
              </svg>
              Username
            </label>
            <!-- svelte-ignore a11y-autofocus -->
            <input
              type="text"
              id="username"
              bind:value={username}
              on:keypress={handleKeyPress}
              disabled={loading}
              required
              autofocus={!loading}
              placeholder="Enter your username"
            />
          </div>

          <div class="form-group">
            <label for="password" class="form-label">
              <svg xmlns="http://www.w3.org/2000/svg" class="input-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z" />
              </svg>
              Password
            </label>
            <input
              type="password"
              id="password"
              bind:value={password}
              on:keypress={handleKeyPress}
              disabled={loading}
              required
              placeholder="Enter your password"
            />
          </div>

          <button
            type="submit"
            class="btn-login"
            disabled={loading}
          >
            {#if loading}
              <Spinner size="small" />
              Logging in...
            {:else}
              <svg xmlns="http://www.w3.org/2000/svg" class="btn-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 16l-4-4m0 0l4-4m-4 4h14m-5 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h7a3 3 0 013 3v1" />
              </svg>
              Sign In
            {/if}
          </button>
        </form>
      </div>
    </div>
  </div>
</div>

<style>
  .login-container {
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 20px;
    background: var(--bg-secondary);
    position: relative;
    overflow: hidden;
  }

  .theme-toggle-login {
    position: absolute;
    top: 20px;
    right: 20px;
    width: 40px;
    height: 40px;
    padding: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    background: var(--bg-primary);
    border: 2px solid var(--accent-color);
    border-radius: 8px;
    color: var(--accent-color);
    cursor: pointer;
    transition: all 0.2s ease;
  }
  .theme-toggle-login:hover {
    background: var(--accent-color);
    color: white;
  }
  .theme-toggle-login svg {
    width: 22px;
    height: 22px;
  }

  .login-wrapper {
    position: relative;
    z-index: 1;
    width: 100%;
    max-width: 420px;
  }

  .login-card {
    background: var(--bg-primary);
    border-radius: 20px;
    box-shadow: var(--shadow-xl);
    border: 1px solid var(--border-color);
    overflow: hidden;
    animation: slideUp 0.5s ease-out;
  }

  @keyframes slideUp {
    from {
      opacity: 0;
      transform: translateY(30px);
    }
    to {
      opacity: 1;
      transform: translateY(0);
    }
  }

  .login-header {
    background: var(--accent-color);
    padding: 40px 30px;
    text-align: center;
    color: white;
  }

  .logo-icon {
    width: 64px;
    height: 64px;
    margin: 0 auto 16px;
    background: rgba(255, 255, 255, 0.2);
    border-radius: 16px;
    display: flex;
    align-items: center;
    justify-content: center;
    backdrop-filter: blur(10px);
  }

  .logo-icon svg {
    width: 36px;
    height: 36px;
  }

  .login-title {
    font-size: 32px;
    font-weight: 700;
    margin: 0 0 8px;
    letter-spacing: -0.5px;
  }

  .login-subtitle {
    font-size: 14px;
    opacity: 0.9;
    margin: 0;
    font-weight: 300;
  }

  .login-body {
    padding: 40px 30px;
  }

  .login-form {
    display: flex;
    flex-direction: column;
    gap: 24px;
  }

  .form-group {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .form-label {
    font-weight: 600;
    font-size: 14px;
    color: var(--text-primary);
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .input-icon {
    width: 18px;
    height: 18px;
    color: var(--primary-color);
  }

  .login-form .form-group input {
    padding: 14px 16px;
    font-size: 15px;
  }

  .btn-login {
    width: 100%;
    padding: 14px 24px;
    background: var(--accent-color);
    color: white;
    border: none;
    border-radius: 10px;
    font-size: 16px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.2s ease;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    margin-top: 8px;
    box-shadow: var(--shadow-md);
  }

  .btn-login:hover:not(:disabled) {
    background: var(--accent-dark);
    transform: translateY(-2px);
    box-shadow: var(--shadow-lg);
  }

  .btn-login:active:not(:disabled) {
    transform: translateY(0);
  }

  .btn-login:disabled {
    opacity: 0.7;
    cursor: not-allowed;
    transform: none;
  }

  .btn-icon {
    width: 20px;
    height: 20px;
  }
</style>

