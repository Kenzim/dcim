<script>
  import PageHeader from './PageHeader.svelte';
  import { 
    getServices, 
    getService,
    getExternalUsers,
    getExternalUser
  } from '../lib/api.js';
  import { onMount } from 'svelte';

  let services = [];
  let externalUsers = [];
  let loading = true;
  let error = null;
  let activeTab = 'services'; // 'services' or 'users'
  let statusFilter = 'all';
  let selectedService = null;
  let selectedUser = null;

  onMount(async () => {
    await Promise.all([loadServices(), loadExternalUsers()]);
  });

  async function loadServices() {
    try {
      loading = true;
      error = null;
      const params = statusFilter !== 'all' ? { status_filter: statusFilter } : {};
      services = await getServices(params);
    } catch (err) {
      error = err.message;
      console.error('Failed to load services:', err);
    } finally {
      loading = false;
    }
  }

  async function loadExternalUsers() {
    try {
      externalUsers = await getExternalUsers();
    } catch (err) {
      console.error('Failed to load external users:', err);
    }
  }

  function getStatusBadgeClass(status) {
    switch (status.toLowerCase()) {
      case 'active':
        return 'status-badge status-active';
      case 'suspended':
        return 'status-badge status-suspended';
      case 'terminated':
        return 'status-badge status-terminated';
      case 'pending':
        return 'status-badge status-pending';
      default:
        return 'status-badge';
    }
  }

  function formatDate(dateString) {
    if (!dateString) return 'N/A';
    return new Date(dateString).toLocaleString();
  }

  async function viewServiceDetails(serviceId) {
    try {
      selectedService = await getService(serviceId);
    } catch (err) {
      alert('Failed to load service details: ' + err.message);
    }
  }

  async function viewUserDetails(userId) {
    try {
      selectedUser = await getExternalUser(userId);
    } catch (err) {
      alert('Failed to load user details: ' + err.message);
    }
  }

  function closeDetails() {
    selectedService = null;
    selectedUser = null;
  }
</script>

<PageHeader title="Services & External Users" />

<div class="services-container">
  <div class="tabs">
    <button 
      class="tab-button" 
      class:active={activeTab === 'services'}
      on:click={() => { activeTab = 'services'; loadServices(); }}
    >
      Services ({services.length})
    </button>
    <button 
      class="tab-button" 
      class:active={activeTab === 'users'}
      on:click={() => { activeTab = 'users'; }}
    >
      External Users ({externalUsers.length})
    </button>
  </div>

  {#if activeTab === 'services'}
    <div class="services-section">
      <div class="section-header">
        <div class="filters">
          <label for="status-filter">Filter by Status:</label>
          <select id="status-filter" bind:value={statusFilter} on:change={loadServices}>
            <option value="all">All</option>
            <option value="active">Active</option>
            <option value="suspended">Suspended</option>
            <option value="terminated">Terminated</option>
            <option value="pending">Pending</option>
          </select>
        </div>
      </div>

      {#if loading}
        <div class="loading">Loading services...</div>
      {:else if error}
        <div class="error">Error: {error}</div>
      {:else if services.length === 0}
        <div class="empty-state">
          <p>No services found.</p>
        </div>
      {:else}
        <div class="services-grid">
          {#each services as service}
            <div class="service-card" on:click={() => viewServiceDetails(service.id)}>
              <div class="service-header">
                <h3>{service.name}</h3>
                <span class={getStatusBadgeClass(service.status)}>{service.status}</span>
              </div>
              <div class="service-details">
                <div class="detail-item">
                  <span class="detail-label">Server:</span>
                  <span>{service.server_name}</span>
                </div>
                <div class="detail-item">
                  <span class="detail-label">External User ID:</span>
                  <span>{service.external_user_external_id}</span>
                </div>
                {#if service.external_service_id}
                  <div class="detail-item">
                    <span class="detail-label">External Service ID:</span>
                    <span>{service.external_service_id}</span>
                  </div>
                {/if}
                <div class="detail-item">
                  <span class="detail-label">Created:</span>
                  <span>{formatDate(service.created_at)}</span>
                </div>
              </div>
            </div>
          {/each}
        </div>
      {/if}
    </div>
  {:else}
    <div class="users-section">
      {#if loading}
        <div class="loading">Loading external users...</div>
      {:else if externalUsers.length === 0}
        <div class="empty-state">
          <p>No external users found.</p>
        </div>
      {:else}
        <div class="users-grid">
          {#each externalUsers as user}
            <div class="user-card" on:click={() => viewUserDetails(user.id)}>
              <div class="user-header">
                <h3>{user.external_username || user.external_user_id}</h3>
                <span class="integration-badge">{user.integration_name}</span>
              </div>
              <div class="user-details">
                <div class="detail-item">
                  <span class="detail-label">External User ID:</span>
                  <span>{user.external_user_id}</span>
                </div>
                {#if user.external_email}
                  <div class="detail-item">
                    <span class="detail-label">Email:</span>
                    <span>{user.external_email}</span>
                  </div>
                {/if}
                <div class="detail-item">
                  <span class="detail-label">Services:</span>
                  <span>{user.service_count}</span>
                </div>
                <div class="detail-item">
                  <span class="detail-label">Created:</span>
                  <span>{formatDate(user.created_at)}</span>
                </div>
              </div>
            </div>
          {/each}
        </div>
      {/if}
    </div>
  {/if}
</div>

{#if selectedService}
  <div class="modal-overlay" on:click={closeDetails}>
    <div class="modal-content" on:click|stopPropagation>
      <div class="modal-header">
        <h3>Service Details</h3>
        <button class="btn-icon-only" on:click={closeDetails}>
          <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
          </svg>
        </button>
      </div>
      <div class="modal-body">
        <div class="detail-group">
          <div class="detail-row">
            <span class="detail-label">Name:</span>
            <span>{selectedService.name}</span>
          </div>
          <div class="detail-row">
            <span class="detail-label">Status:</span>
            <span class={getStatusBadgeClass(selectedService.status)}>{selectedService.status}</span>
          </div>
          <div class="detail-row">
            <span class="detail-label">Server:</span>
            <span>{selectedService.server_name} (ID: {selectedService.server_id})</span>
          </div>
          <div class="detail-row">
            <span class="detail-label">External User ID:</span>
            <span>{selectedService.external_user_external_id}</span>
          </div>
          {#if selectedService.external_service_id}
            <div class="detail-row">
              <span class="detail-label">External Service ID:</span>
              <span>{selectedService.external_service_id}</span>
            </div>
          {/if}
          {#if selectedService.description}
            <div class="detail-row">
              <span class="detail-label">Description:</span>
              <span>{selectedService.description}</span>
            </div>
          {/if}
          <div class="detail-row">
            <span class="detail-label">Created:</span>
            <span>{formatDate(selectedService.created_at)}</span>
          </div>
          <div class="detail-row">
            <span class="detail-label">Updated:</span>
            <span>{formatDate(selectedService.updated_at)}</span>
          </div>
          {#if selectedService.terminated_at}
            <div class="detail-row">
              <span class="detail-label">Terminated:</span>
              <span>{formatDate(selectedService.terminated_at)}</span>
            </div>
          {/if}
        </div>
      </div>
      <div class="modal-footer">
        <button class="btn-secondary" on:click={closeDetails}>Close</button>
      </div>
    </div>
  </div>
{/if}

{#if selectedUser}
  <div class="modal-overlay" on:click={closeDetails}>
    <div class="modal-content" on:click|stopPropagation>
      <div class="modal-header">
        <h3>External User Details</h3>
        <button class="btn-icon-only" on:click={closeDetails}>
          <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
          </svg>
        </button>
      </div>
      <div class="modal-body">
        <div class="detail-group">
          <div class="detail-row">
            <span class="detail-label">External User ID:</span>
            <span>{selectedUser.external_user_id}</span>
          </div>
          {#if selectedUser.external_username}
            <div class="detail-row">
              <span class="detail-label">Username:</span>
              <span>{selectedUser.external_username}</span>
            </div>
          {/if}
          {#if selectedUser.external_email}
            <div class="detail-row">
              <span class="detail-label">Email:</span>
              <span>{selectedUser.external_email}</span>
            </div>
          {/if}
          <div class="detail-row">
            <span class="detail-label">Integration:</span>
            <span>{selectedUser.integration_name} (ID: {selectedUser.integration_id})</span>
          </div>
          <div class="detail-row">
            <span class="detail-label">Services:</span>
            <span>{selectedUser.service_count}</span>
          </div>
          <div class="detail-row">
            <span class="detail-label">Created:</span>
            <span>{formatDate(selectedUser.created_at)}</span>
          </div>
          <div class="detail-row">
            <span class="detail-label">Updated:</span>
            <span>{formatDate(selectedUser.updated_at)}</span>
          </div>
        </div>
      </div>
      <div class="modal-footer">
        <button class="btn-secondary" on:click={closeDetails}>Close</button>
      </div>
    </div>
  </div>
{/if}

<style>
  .services-container {
    padding: 32px;
  }

  .tabs {
    display: flex;
    gap: 8px;
    margin-bottom: 24px;
    border-color: var(--border-color);
  }

  .tab-button {
    padding: 12px 24px;
    background: none;
    border: none;
    border-bottom: 2px solid transparent;
    font-weight: 600;
    color: var(--text-secondary);
    cursor: pointer;
    transition: color 0.2s ease, border-color 0.2s ease;
    margin-bottom: -2px;
  }

  .tab-button:hover {
    color: var(--text-primary);
  }

  .tab-button.active {
    color: var(--accent-color);
    border-bottom-color: var(--accent-color);
  }

  .section-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 24px;
  }

  .filters {
    display: flex;
    align-items: center;
    gap: 12px;
  }

  .filters label {
    font-weight: 600;
    color: var(--text-primary);
  }

  .filters select {
    padding: 8px 12px;
    border-color: var(--border-color);
    border-radius: 8px;
    font-size: 14px;
  }

  .loading, .error, .empty-state {
    text-align: center;
    padding: 48px;
    color: var(--text-secondary);
  }

  .error {
    color: #ef4444;
  }

  .services-grid, .users-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(350px, 1fr));
    gap: 20px;
  }

  .service-card, .user-card {
    background: var(--bg-primary);
    border-radius: 12px;
    padding: 20px;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
    transition: transform 0.2s ease, box-shadow 0.2s ease;
    cursor: pointer;
  }

  .service-card:hover, .user-card:hover {
    transform: translateY(-2px);
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
  }

  .service-header, .user-header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    margin-bottom: 12px;
  }

  .service-header h3, .user-header h3 {
    margin: 0;
    font-size: 18px;
    font-weight: 600;
    color: var(--text-primary);
  }

  .status-badge {
    padding: 4px 12px;
    border-radius: 12px;
    font-size: 12px;
    font-weight: 600;
    text-transform: capitalize;
  }

  .status-active {
    background: #d1fae5;
    color: #065f46;
  }

  .status-suspended {
    background: #fef3c7;
    color: #92400e;
  }

  .status-terminated {
    background: #fee2e2;
    color: #991b1b;
  }

  .status-pending {
    background: #dbeafe;
    color: #1e40af;
  }

  .integration-badge {
    padding: 4px 12px;
    border-radius: 12px;
    font-size: 12px;
    font-weight: 600;
    background: #e0e7ff;
    color: #3730a3;
  }

  .service-details, .user-details {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .detail-item {
    display: flex;
    justify-content: space-between;
    font-size: 14px;
  }

  .detail-label {
    font-weight: 600;
    color: var(--text-secondary);
  }

  .modal-overlay {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background: rgba(0, 0, 0, 0.5);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
  }

  .modal-content {
    background: var(--bg-primary);
    border-radius: 12px;
    width: 90%;
    max-width: 600px;
    max-height: 90vh;
    overflow-y: auto;
    box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.1);
  }

  .modal-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 20px 24px;
    border-color: var(--border-color);
  }

  .modal-header h3 {
    margin: 0;
    font-size: 20px;
    font-weight: 600;
  }

  .modal-body {
    padding: 24px;
  }

  .detail-group {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .detail-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding-bottom: 12px;
    border-bottom: 1px solid #f1f5f9;
  }

  .detail-row:last-child {
    border-bottom: none;
  }

  .modal-footer {
    display: flex;
    justify-content: flex-end;
    gap: 12px;
    padding: 20px 24px;
    border-color: var(--border-color);
  }

  .btn-secondary {
    padding: 10px 20px;
    background: var(--bg-tertiary);
    color: var(--text-primary);
    border: none;
    border-radius: 8px;
    font-weight: 600;
    cursor: pointer;
    transition: background 0.2s ease;
  }

  .btn-secondary:hover {
    background: var(--accent-color);
  }

  .btn-icon-only {
    background: var(--bg-tertiary);
    border: 1px solid var(--border-color);
    padding: 6px;
    cursor: pointer;
    color: var(--text-primary);
    border-radius: 6px;
    transition: background 0.2s ease, color 0.2s ease;
  }

  .btn-icon-only:hover {
    background: var(--bg-secondary);
    border-color: var(--accent-color);
    color: var(--accent-color);
    transform: translateY(-1px);
    color: var(--text-primary);
  }

  .btn-icon-only svg {
    width: 18px;
    height: 18px;
  }
</style>
