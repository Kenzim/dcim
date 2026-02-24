<script>
  import PageHeader from './PageHeader.svelte';
  import { Button, Modal, FormGroup, FormError, Alert } from './ui/index.js';
  import { getServerGroups, createServerGroup, updateServerGroup, deleteServerGroup } from '../lib/api.js';
  import { onMount } from 'svelte';
  let groups = [];
  let loading = true;
  let error = null;
  let showModal = false;
  let editingGroup = null;
  let formData = {
    name: '',
    description: ''
  };
  let formError = null;

  onMount(async () => {
    await loadGroups();
  });

  async function loadGroups() {
    try {
      loading = true;
      error = null;
      groups = await getServerGroups();
    } catch (err) {
      error = err.message;
      console.error('Failed to load server groups:', err);
    } finally {
      loading = false;
    }
  }

  function openModal(group = null) {
    console.log('openModal called', group);
    editingGroup = group;
    if (group) {
      formData = {
        name: group.name,
        description: group.description || ''
      };
    } else {
      formData = {
        name: '',
        description: ''
      };
    }
    formError = null;
    showModal = true;
    console.log('showModal set to:', showModal);
  }

  function closeModal() {
    showModal = false;
    editingGroup = null;
    formData = {
      name: '',
      description: ''
    };
    formError = null;
  }

  async function handleSubmit() {
    if (!formData.name.trim()) {
      formError = 'Name is required';
      return;
    }

    try {
      formError = null;
      if (editingGroup) {
        await updateServerGroup(editingGroup.id, { name: formData.name, description: formData.description || null });
      } else {
        await createServerGroup(formData.name, formData.description || null);
      }
      closeModal();
      await loadGroups();
    } catch (err) {
      formError = err.message;
    }
  }

  async function handleDelete(group) {
    if (!confirm(`Are you sure you want to delete server group "${group.name}"?`)) {
      return;
    }

    try {
      await deleteServerGroup(group.id);
      await loadGroups();
    } catch (err) {
      alert('Failed to delete server group: ' + err.message);
    }
  }
</script>

<PageHeader title="Server Groups" />

<div class="server-groups-container">
  <div class="server-groups-header">
    <h2>Server Groups</h2>
    <Button variant="primary" on:click={() => openModal()}>
      <svelte:fragment slot="icon">
        <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
        </svg>
      </svelte:fragment>
      Add Server Group
    </Button>
  </div>

  {#if error}
    <Alert type="danger">{error}</Alert>
  {/if}

  {#if loading}
    <div class="loading">Loading server groups...</div>
  {:else if groups.length === 0}
    <div class="empty-state">
      <p>No server groups found. Create your first server group to organize servers.</p>
    </div>
  {:else}
    <table class="table server-groups-table">
      <thead>
        <tr>
          <th>Name</th>
          <th>Description</th>
          <th>Servers</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody>
        {#each groups as group}
          <tr>
            <td class="name-cell">
              <a href="/admin/server-groups/{group.id}" class="group-name-link">{group.name}</a>
            </td>
            <td class="description-cell">
              {#if group.description}
                {group.description}
              {:else}
                <span class="empty-text">-</span>
              {/if}
            </td>
            <td class="count-cell">
              <span class="server-count-badge">{group.server_count}</span>
            </td>
            <td class="actions-cell">
              <div class="action-buttons">
                <Button variant="secondary" size="small" on:click={() => openModal(group)}>Edit</Button>
                <Button variant="danger" size="small" on:click={() => handleDelete(group)}>Delete</Button>
              </div>
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}
</div>

{#if showModal}
  <Modal title={editingGroup ? 'Edit Server Group' : 'Add Server Group'} onClose={closeModal}>
    <form id="server-group-form" on:submit|preventDefault={handleSubmit}>
      {#if formError}
        <FormError>{formError}</FormError>
      {/if}
      <FormGroup label="Name *" forId="name" required>
        <input type="text" id="name" bind:value={formData.name} required />
      </FormGroup>
      <FormGroup label="Description" forId="description">
        <textarea id="description" rows="3" bind:value={formData.description} />
      </FormGroup>
    </form>
    <svelte:fragment slot="footer">
      <Button variant="secondary" on:click={closeModal}>Cancel</Button>
      <Button type="submit" form="server-group-form" variant="primary">
        {editingGroup ? 'Update' : 'Create'}
      </Button>
    </svelte:fragment>
  </Modal>
{/if}

<style>
  .server-groups-container {
    padding: 20px;
    background: var(--bg-secondary);
    color: var(--text-primary);
    min-height: 100%;
  }

  .server-groups-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 24px;
    padding-bottom: 16px;
    border-bottom: 1px solid var(--border-color);
  }

  .server-groups-header h2 {
    color: var(--text-primary);
    font-size: 28px;
    font-weight: 600;
    margin: 0;
  }

  .loading {
    text-align: center;
    padding: 40px;
    color: var(--text-secondary);
    font-size: 16px;
  }

  .empty-state {
    text-align: center;
    padding: 40px;
    color: var(--text-secondary);
  }

  .empty-state p {
    font-size: 16px;
    margin: 0;
  }

  /* Table styles */
  .server-groups-table {
    box-shadow: var(--shadow-md);
  }

  .server-groups-table {
    width: 100%;
    border-collapse: collapse;
    background: var(--bg-primary);
    border-radius: 12px;
    overflow: hidden;
    border: 1px solid var(--border-color);
  }

  .server-groups-table thead {
    background: var(--bg-tertiary);
  }

  .server-groups-table thead th {
    padding: 14px 16px;
    text-align: left;
    font-weight: 600;
    color: var(--text-primary);
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    border-bottom: 2px solid var(--border-color);
    background: var(--bg-tertiary);
  }

  .server-groups-table thead th:last-child {
    text-align: center;
  }

  .server-groups-table td {
    padding: 16px;
    border-top: 1px solid var(--border-color);
    color: var(--text-primary);
    background: var(--bg-primary);
    vertical-align: middle;
  }

  .server-groups-table tbody tr {
    transition: background-color 0.2s ease;
    background: var(--bg-primary);
  }

  .server-groups-table tbody tr:hover {
    background: var(--bg-tertiary);
  }

  .name-cell {
    font-weight: 500;
  }

  .group-name-link {
    color: var(--accent-color);
    text-decoration: none;
    font-size: 15px;
    font-weight: 500;
    transition: all 0.2s ease;
    display: inline-block;
  }

  .group-name-link:hover {
    color: var(--accent-light, #4dd0e1);
    text-decoration: underline;
    transform: translateX(2px);
  }

  .description-cell {
    color: var(--text-secondary);
    font-size: 14px;
  }

  .empty-text {
    color: var(--text-tertiary);
    font-style: italic;
  }

  .count-cell {
    text-align: center;
  }

  .server-count-badge {
    display: inline-block;
    padding: 4px 12px;
    background: var(--bg-tertiary);
    color: var(--accent-color);
    border: 1px solid var(--accent-color);
    border-radius: 12px;
    font-weight: 600;
    font-size: 13px;
    min-width: 40px;
    text-align: center;
  }

  .actions-cell {
    width: 200px;
  }

  .action-buttons {
    display: flex;
    gap: 8px;
    align-items: center;
  }

</style>
