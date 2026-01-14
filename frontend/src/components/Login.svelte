<script>
  import { login } from '../lib/api.js';
  import { user, isAuthenticated } from '../stores/auth.js';

  let username = '';
  let password = '';
  let error = '';
  let loading = false;

  async function handleLogin() {
    error = '';
    loading = true;
    console.log('Starting login for user:', username);

    try {
      console.log('Calling login API...');
      const result = await login(username, password);
      console.log('Login API response:', result);
      
      // Cookie is set automatically by the server
      // Update store directly from login response
      // Dashboard will fetch full user data when it loads
      user.set({
        id: result.user_id,
        username: result.username,
        email: '', // Will be fetched by Dashboard from /me endpoint
        is_admin: result.is_admin
      });
      isAuthenticated.set(true);
      console.log('Store updated, isAuthenticated set to true');
      loading = false;
      // App.svelte will reactively show Dashboard component
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
  <div class="login-wrapper">
    <div class="login-card">
      <div class="login-header">
        <div class="logo-icon">
          <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" />
          </svg>
        </div>
        <h1 class="login-title">DCIM</h1>
        <p class="login-subtitle">Data Center Infrastructure Management</p>
      </div>
      
      <div class="login-body">
        {#if error}
          <div class="alert alert-danger" role="alert">
            <svg xmlns="http://www.w3.org/2000/svg" class="alert-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
            {error}
          </div>
        {/if}

        <form on:submit|preventDefault={handleLogin} class="login-form">
          <div class="form-group">
            <label for="username" class="form-label">
              <svg xmlns="http://www.w3.org/2000/svg" class="input-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
              </svg>
              Username
            </label>
            <input
              type="text"
              class="form-control"
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
              class="form-control"
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
              <span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>
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
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    position: relative;
    overflow: hidden;
  }

  .login-container::before {
    content: '';
    position: absolute;
    top: -50%;
    right: -50%;
    width: 200%;
    height: 200%;
    background: radial-gradient(circle, rgba(255, 255, 255, 0.1) 1px, transparent 1px);
    background-size: 50px 50px;
    animation: drift 20s linear infinite;
  }

  @keyframes drift {
    0% { transform: translate(0, 0); }
    100% { transform: translate(50px, 50px); }
  }

  .login-wrapper {
    position: relative;
    z-index: 1;
    width: 100%;
    max-width: 420px;
  }

  .login-card {
    background: white;
    border-radius: 20px;
    box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
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
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
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

  .alert {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 12px 16px;
    border-radius: 10px;
    margin-bottom: 24px;
    background: #fee2e2;
    color: #991b1b;
    border: 1px solid #fecaca;
  }

  .alert-icon {
    width: 20px;
    height: 20px;
    flex-shrink: 0;
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

  .form-control {
    width: 100%;
    padding: 14px 16px;
    border: 2px solid var(--border-color);
    border-radius: 10px;
    font-size: 15px;
    transition: all 0.2s ease;
    background: white;
  }

  .form-control:focus {
    outline: none;
    border-color: var(--primary-color);
    box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.1);
  }

  .form-control:disabled {
    background: #f1f5f9;
    cursor: not-allowed;
  }

  .btn-login {
    width: 100%;
    padding: 14px 24px;
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
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
    box-shadow: 0 4px 12px rgba(102, 126, 234, 0.4);
  }

  .btn-login:hover:not(:disabled) {
    transform: translateY(-2px);
    box-shadow: 0 6px 20px rgba(102, 126, 234, 0.5);
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

