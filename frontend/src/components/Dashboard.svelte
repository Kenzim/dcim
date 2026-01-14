<script>
  import { onMount } from 'svelte';
  import { user, logout, checkAuth } from '../stores/auth.js';
  import { getCurrentUser } from '../lib/api.js';

  let userData = null;
  let loading = true;
  let error = null;

  onMount(async () => {
    await loadUserData();
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

  async function handleLogout() {
    try {
      await logout();
    } catch (error) {
      console.error('Logout error:', error);
    } finally {
      // Always clear local state and redirect
      userData = null;
      // The logout() function already updates the store, which will trigger App.svelte to show Login
      // But we can also force a redirect to ensure clean state
      window.location.href = '/';
    }
  }
</script>

<div class="container-fluid">
  <div class="row">
    <!-- Sidebar -->
    <nav class="col-md-3 col-lg-2 d-md-block bg-light sidebar vh-100">
      <div class="position-sticky pt-3">
        <h4 class="px-3">DCIM</h4>
        <hr>
        <ul class="nav flex-column">
          <li class="nav-item">
            <span class="nav-link active" role="button" tabindex="0">
              Dashboard
            </span>
          </li>
        </ul>
        <hr>
        <div class="px-3">
          <button class="btn btn-outline-danger btn-sm w-100" on:click={handleLogout}>
            Logout
          </button>
        </div>
      </div>
    </nav>

    <!-- Main content -->
    <main class="col-md-9 ms-sm-auto col-lg-10 px-md-4">
      <div class="d-flex justify-content-between flex-wrap flex-md-nowrap align-items-center pt-3 pb-2 mb-3 border-bottom">
        <h1 class="h2">Dashboard</h1>
      </div>

      {#if loading}
        <div class="text-center py-5">
          <div class="spinner-border" role="status">
            <span class="visually-hidden">Loading...</span>
          </div>
        </div>
      {:else if error}
        <div class="alert alert-danger" role="alert">
          {error}
        </div>
      {:else if userData}
        <div class="card">
          <div class="card-header">
            <h5 class="card-title mb-0">User Information (from /me endpoint)</h5>
          </div>
          <div class="card-body">
            <dl class="row">
              <dt class="col-sm-3">ID:</dt>
              <dd class="col-sm-9">{userData.id}</dd>

              <dt class="col-sm-3">Username:</dt>
              <dd class="col-sm-9">{userData.username}</dd>

              <dt class="col-sm-3">Email:</dt>
              <dd class="col-sm-9">{userData.email}</dd>

              <dt class="col-sm-3">Admin:</dt>
              <dd class="col-sm-9">
                {#if userData.is_admin}
                  <span class="badge bg-success">Yes</span>
                {:else}
                  <span class="badge bg-secondary">No</span>
                {/if}
              </dd>
            </dl>
          </div>
        </div>
      {/if}
    </main>
  </div>
</div>

<style>
  .sidebar {
    position: fixed;
    top: 0;
    bottom: 0;
    left: 0;
    z-index: 100;
    padding: 48px 0 0;
    box-shadow: inset -1px 0 0 rgba(0, 0, 0, 0.1);
  }

  .sidebar .nav-link {
    color: #333;
  }

  .sidebar .nav-link.active {
    color: #007bff;
    font-weight: 500;
  }

  main {
    margin-left: 0;
  }

  @media (min-width: 768px) {
    main {
      margin-left: 240px;
    }
  }
</style>

