<script>
  import { onMount } from 'svelte';
  import PageHeader from './PageHeader.svelte';
  import {
    listIpamSubnets,
    createIpamSubnet,
    getServices,
    assignIpamAddress,
    listIpamHistory,
    releaseIpamAssignment,
    listServiceIpAssignments,
  } from '../lib/api.js';

  let loading = false;
  let error = '';
  let subnets = [];
  let services = [];
  let history = [];
  let serviceAssignments = [];

  let subnetForm = { name: '', cidr: '', location_id: '', allocation_strategy: 'first_free' };
  let assignForm = { service_id: '', subnet_id: '', strategy: '', username: '', password: '' };

  async function loadAll() {
    loading = true;
    error = '';
    try {
      subnets = await listIpamSubnets();
      services = (await getServices()).filter(s => s.service_type === 'http_proxy');
      history = await listIpamHistory();
      serviceAssignments = [];
      if (assignForm.service_id) {
        serviceAssignments = await listServiceIpAssignments(Number(assignForm.service_id));
      }
    } catch (err) {
      error = err.message;
    } finally {
      loading = false;
    }
  }

  async function submitSubnet() {
    try {
      await createIpamSubnet({
        ...subnetForm,
        location_id: subnetForm.location_id ? Number(subnetForm.location_id) : null,
      });
      subnetForm = { ...subnetForm, name: '', cidr: '', location_id: '' };
      await loadAll();
    } catch (err) {
      error = err.message;
    }
  }

  async function submitAssignment() {
    try {
      await assignIpamAddress({
        ...assignForm,
        service_id: Number(assignForm.service_id),
        subnet_id: assignForm.subnet_id ? Number(assignForm.subnet_id) : null,
        strategy: assignForm.strategy || null,
      });
      await loadAll();
    } catch (err) {
      error = err.message;
    }
  }

  async function releaseAssignment(id) {
    try {
      await releaseIpamAssignment(id);
      await loadAll();
    } catch (err) {
      error = err.message;
    }
  }

  $: if (assignForm.service_id) {
    listServiceIpAssignments(Number(assignForm.service_id))
      .then((rows) => (serviceAssignments = rows))
      .catch(() => (serviceAssignments = []));
  }

  onMount(loadAll);
</script>

<PageHeader title="IPAM & Proxy" />
<div class="page">
  {#if error}<div class="error">{error}</div>{/if}

  <section>
    <h3>Create Subnet</h3>
    <input bind:value={subnetForm.name} placeholder="Subnet name" />
    <input bind:value={subnetForm.cidr} placeholder="198.51.100.0/24" />
    <input bind:value={subnetForm.location_id} placeholder="Location ID (optional)" />
    <select bind:value={subnetForm.allocation_strategy}>
      <option value="first_free">first_free</option>
      <option value="spread_subnets">spread_subnets</option>
      <option value="least_recently_used">least_recently_used</option>
    </select>
    <button on:click={submitSubnet}>Create Subnet</button>
  </section>

  <section>
    <h3>Assign Proxy IP</h3>
    <select bind:value={assignForm.service_id}>
      <option value="">Select proxy service</option>
      {#each services as service}
        <option value={service.id}>{service.id} - {service.name}</option>
      {/each}
    </select>
    <select bind:value={assignForm.subnet_id}>
      <option value="">Any subnet</option>
      {#each subnets as subnet}
        <option value={subnet.id}>{subnet.cidr} ({subnet.name})</option>
      {/each}
    </select>
    <input bind:value={assignForm.strategy} placeholder="Optional strategy override" />
    <input bind:value={assignForm.username} placeholder="Proxy username" />
    <input bind:value={assignForm.password} placeholder="Proxy password" />
    <button on:click={submitAssignment} disabled={!assignForm.service_id}>Assign</button>
  </section>

  <section>
    <h3>Service Assignments</h3>
    {#if loading}
      <p>Loading...</p>
    {:else if serviceAssignments.length === 0}
      <p>No assignments yet for selected service.</p>
    {:else}
      {#each serviceAssignments as a}
        <div class="card">
          <div><strong>{a.ip_address}</strong> ({a.username})</div>
          <button on:click={() => releaseAssignment(a.id)}>Release</button>
        </div>
      {/each}
    {/if}
  </section>

  <section>
    <h3>Assignment History</h3>
    {#each history as h}
      <div class="history-row">
        <strong>{h.action}</strong> - {h.ip_address} - service {h.service_id ?? 'n/a'}
      </div>
    {/each}
  </section>
</div>

<style>
  .page { padding: 24px; display: flex; flex-direction: column; gap: 16px; }
  section { background: var(--bg-primary); border: 1px solid var(--border-color); border-radius: 10px; padding: 16px; display: flex; flex-direction: column; gap: 8px; }
  input, select { background: var(--bg-secondary); border: 1px solid var(--border-color); color: var(--text-primary); border-radius: 6px; padding: 8px; }
  button { width: fit-content; padding: 8px 12px; border: none; border-radius: 6px; background: var(--accent-color); color: white; cursor: pointer; }
  .card { display: flex; justify-content: space-between; align-items: center; padding: 10px; border: 1px solid var(--border-color); border-radius: 8px; }
  .history-row { padding: 8px; border-bottom: 1px solid var(--border-color); }
  .error { color: var(--danger-color); }
</style>
