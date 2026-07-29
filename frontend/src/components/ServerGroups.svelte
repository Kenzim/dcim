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

<PageHeader title="Server Groups">
  <svelte:fragment slot="actions">
    <Button variant="primary" on:click={() => openModal()}>
      <svelte:fragment slot="icon">
        <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
        </svg>
      </svelte:fragment>
      Add Server Group
    </Button>
  </svelte:fragment>
</PageHeader>

<div class="admin-page server-groups-container">
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
    <div class="table-scroll admin-data-table">
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
    </div>
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
  .loading,
  .empty-state {
    text-align: center;
    padding: 40px;
    color: var(--text-secondary);
  }

  .empty-state p {
    font-size: 14px;
    margin: 0;
  }

  .server-groups-table {
    min-width: 560px;
  }

  .server-groups-table thead th:last-child {
    text-align: center;
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
