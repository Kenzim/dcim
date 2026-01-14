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

<div class="container">
  <div class="row justify-content-center">
    <div class="col-md-4 col-lg-3">
      <div class="card mt-5">
        <div class="card-body">
          <h2 class="card-title text-center mb-4">Login</h2>
          
          {#if error}
            <div class="alert alert-danger" role="alert">
              {error}
            </div>
          {/if}

          <form on:submit|preventDefault={handleLogin}>
            <div class="mb-3">
              <label for="username" class="form-label">Username</label>
              <input
                type="text"
                class="form-control"
                id="username"
                bind:value={username}
                on:keypress={handleKeyPress}
                disabled={loading}
                required
                autofocus={!loading}
              />
            </div>

            <div class="mb-3">
              <label for="password" class="form-label">Password</label>
              <input
                type="password"
                class="form-control"
                id="password"
                bind:value={password}
                on:keypress={handleKeyPress}
                disabled={loading}
                required
              />
            </div>

            <button
              type="submit"
              class="btn btn-primary w-100"
              disabled={loading}
            >
              {#if loading}
                <span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>
                Logging in...
              {:else}
                Login
              {/if}
            </button>
          </form>
        </div>
      </div>
    </div>
  </div>
</div>

