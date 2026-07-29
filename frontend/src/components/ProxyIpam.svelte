<script>
  import { onMount } from 'svelte';
  import PageHeader from './PageHeader.svelte';
  import Button from './ui/Button.svelte';
  import { navigate } from '../lib/router.js';
  import {
    listIpamSubnets,
    createIpamSubnet,
    updateIpamSubnet,
    deleteIpamSubnet,
    listIpamAssignments,
    getServices,
    getLocations,
    assignIpamAddress,
    listIpamHistory,
    releaseIpamAssignment,
  } from '../lib/api.js';

  const PROXY_PORT = 8080;

  let loading = false;
  let error = '';
  let success = '';
  let subnets = [];
  let services = [];
  let locations = [];
  let assignments = [];
  let history = [];
  let historyServiceFilter = '';
  let showCreateSubnet = false;
  let revealedPasswords = {};

  let subnetForm = {
    name: '',
    cidr: '',
    location_id: '',
    range_start: '',
    range_end: '',
    allocation_strategy: 'first_free',
    max_resale_count: 1,
  };
  let assignForm = {
    service_id: '',
    subnet_id: '',
    strategy: '',
    username: '',
    password: '',
  };

  $: locationName = (id) => {
    if (id == null) return '—';
    const loc = locations.find((l) => l.id === Number(id));
    return loc ? loc.name : `Location #${id}`;
  };

  $: totalAssigned = subnets.reduce((n, s) => n + (s.assigned_ips || 0), 0);
  $: totalIps = subnets.reduce((n, s) => n + (s.total_ips || 0), 0);
  $: freeIps = Math.max(0, totalIps - totalAssigned);
  $: totalSlots = subnets.reduce((n, s) => n + (s.total_slots || 0), 0);
  $: usedSlots = subnets.reduce((n, s) => n + (s.used_slots || 0), 0);

  $: filteredHistory = historyServiceFilter
    ? history.filter((h) => String(h.service_id) === String(historyServiceFilter))
    : history;

  function randomCreds() {
    const alphabet = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789';
    const pick = (n) => Array.from({ length: n }, () => alphabet[Math.floor(Math.random() * alphabet.length)]).join('');
    assignForm.username = 'px' + pick(8);
    assignForm.password = pick(20);
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      success = 'Copied to clipboard';
      setTimeout(() => { if (success === 'Copied to clipboard') success = ''; }, 2000);
    } catch {
      error = 'Clipboard unavailable';
    }
  }

  function proxyUrls(a) {
    const u = encodeURIComponent(a.username || '');
    const p = encodeURIComponent(a.password || '');
    const host = a.ip_address;
    return {
      http: `http://${u}:${p}@${host}:${PROXY_PORT}`,
      socks5: `socks5://${u}:${p}@${host}:${PROXY_PORT}`,
    };
  }

  async function loadAll() {
    loading = true;
    error = '';
    try {
      const [subnetRows, serviceRows, locationRows, assignmentRows, historyRows] = await Promise.all([
        listIpamSubnets(),
        getServices({ service_type: 'http_proxy' }),
        getLocations(),
        listIpamAssignments(),
        listIpamHistory(),
      ]);
      subnets = subnetRows;
      services = Array.isArray(serviceRows) ? serviceRows.filter((s) => s.service_type === 'http_proxy') : [];
      locations = locationRows || [];
      assignments = assignmentRows || [];
      history = historyRows || [];
    } catch (err) {
      error = err.message;
    } finally {
      loading = false;
    }
  }

  async function submitSubnet() {
    error = '';
    try {
      await createIpamSubnet({
        name: subnetForm.name,
        cidr: subnetForm.cidr,
        location_id: subnetForm.location_id ? Number(subnetForm.location_id) : null,
        range_start: subnetForm.range_start || null,
        range_end: subnetForm.range_end || null,
        allocation_strategy: subnetForm.allocation_strategy,
        max_resale_count: Number(subnetForm.max_resale_count) || 1,
      });
      subnetForm = {
        name: '',
        cidr: '',
        location_id: '',
        range_start: '',
        range_end: '',
        allocation_strategy: 'first_free',
        max_resale_count: 1,
      };
      showCreateSubnet = false;
      success = 'Subnet created';
      await loadAll();
    } catch (err) {
      error = err.message;
    }
  }

  async function toggleSubnetEnabled(subnet) {
    error = '';
    try {
      await updateIpamSubnet(subnet.id, { enabled: !subnet.enabled });
      await loadAll();
    } catch (err) {
      error = err.message;
    }
  }

  async function updateMaxResale(subnet, value) {
    const parsed = Number(value);
    if (!Number.isFinite(parsed) || parsed < 1 || parsed === subnet.max_resale_count) return;
    error = '';
    try {
      await updateIpamSubnet(subnet.id, { max_resale_count: parsed });
      success = `Resale cap updated for ${subnet.cidr}`;
      await loadAll();
    } catch (err) {
      error = err.message;
    }
  }

  async function removeSubnet(subnet) {
    if (!confirm(`Delete subnet ${subnet.cidr}?`)) return;
    error = '';
    try {
      await deleteIpamSubnet(subnet.id);
      success = 'Subnet deleted';
      await loadAll();
    } catch (err) {
      error = err.message;
    }
  }

  async function submitAssignment() {
    error = '';
    try {
      const payload = {
        service_id: Number(assignForm.service_id),
        subnet_id: assignForm.subnet_id ? Number(assignForm.subnet_id) : null,
        strategy: assignForm.strategy || null,
        username: assignForm.username || null,
        password: assignForm.password || null,
        assigned_by: 'admin-ui',
      };
      const created = await assignIpamAddress(payload);
      success = `Assigned ${created.ip_address} (${created.username})`;
      assignForm = { ...assignForm, username: '', password: '' };
      await loadAll();
    } catch (err) {
      error = err.message;
    }
  }

  async function releaseAssignment(id) {
    if (!confirm('Release this IP assignment?')) return;
    error = '';
    try {
      await releaseIpamAssignment(id);
      success = 'Assignment released';
      await loadAll();
    } catch (err) {
      error = err.message;
    }
  }

  function formatDate(value) {
    if (!value) return '—';
    try {
      return new Date(value).toLocaleString();
    } catch {
      return value;
    }
  }

  onMount(loadAll);
</script>

<PageHeader title="IPAM & Proxy" />
<div class="page">
  {#if error}<div class="error">{error}</div>{/if}
  {#if success}<div class="success">{success}</div>{/if}

  <div class="summary">
    <div class="stat"><span class="label">Subnets</span><span class="value">{subnets.length}</span></div>
    <div class="stat"><span class="label">IPs assigned</span><span class="value">{totalAssigned}</span></div>
    <div class="stat"><span class="label">IPs free</span><span class="value">{freeIps}</span></div>
    <div class="stat"><span class="label">Resale slots used</span><span class="value">{usedSlots}/{totalSlots}</span></div>
    <div class="stat"><span class="label">Proxy services</span><span class="value">{services.length}</span></div>
  </div>

  <div class="actions">
    <Button on:click={() => (showCreateSubnet = true)}>Create Subnet</Button>
    <Button variant="secondary" on:click={loadAll} disabled={loading}>Refresh</Button>
  </div>

  <section>
    <h3>Subnets</h3>
    {#if loading && !subnets.length}
      <p>Loading...</p>
    {:else}
      <div class="table subnet-table">
        <div class="row head subnet-row">
          <div>Name</div><div>CIDR</div><div>Location</div><div>Strategy</div><div>Enabled</div><div>IPs</div><div>Resale cap</div><div>Slots used</div><div>Actions</div>
        </div>
        {#each subnets as subnet}
          <div class="row subnet-row">
            <div>{subnet.name}</div>
            <div class="mono">{subnet.cidr}</div>
            <div>{locationName(subnet.location_id)}</div>
            <div>{subnet.allocation_strategy}</div>
            <div>{subnet.enabled ? 'yes' : 'no'}</div>
            <div>{subnet.assigned_ips}/{subnet.total_ips}</div>
            <div>
              <input
                type="number"
                min="1"
                class="resale-input"
                value={subnet.max_resale_count}
                title="How many customers may simultaneously share one IP in this subnet"
                on:change={(e) => updateMaxResale(subnet, e.target.value)}
              />
            </div>
            <div>{subnet.used_slots ?? '—'}/{subnet.total_slots ?? '—'}</div>
            <div class="row-actions">
              <button class="tiny" on:click={() => toggleSubnetEnabled(subnet)}>
                {subnet.enabled ? 'Disable' : 'Enable'}
              </button>
              <button
                class="tiny danger"
                disabled={subnet.assigned_ips > 0}
                title={subnet.assigned_ips > 0 ? 'Release assignments first' : 'Delete subnet'}
                on:click={() => removeSubnet(subnet)}
              >Delete</button>
            </div>
          </div>
        {:else}
          <div class="empty-row">No subnets yet.</div>
        {/each}
      </div>
    {/if}
  </section>

  <section>
    <h3>Assign Proxy IP</h3>
    <div class="form-grid">
      <select bind:value={assignForm.service_id}>
        <option value="">Select proxy service</option>
        {#each services as service}
          <option value={service.id}>{service.id} — {service.name}</option>
        {/each}
      </select>
      <select bind:value={assignForm.subnet_id}>
        <option value="">Any enabled subnet</option>
        {#each subnets.filter((s) => s.enabled) as subnet}
          <option value={subnet.id}>
            {subnet.cidr} ({subnet.name}) — {subnet.used_slots ?? subnet.assigned_ips}/{subnet.total_slots ?? subnet.total_ips} slots
          </option>
        {/each}
      </select>
      <select bind:value={assignForm.strategy}>
        <option value="">Subnet default strategy</option>
        <option value="first_free">first_free</option>
        <option value="spread_subnets">spread_subnets</option>
        <option value="least_recently_used">least_recently_used</option>
      </select>
      <input bind:value={assignForm.username} placeholder="Username (optional — auto if empty)" />
      <input bind:value={assignForm.password} placeholder="Password (optional — auto if empty)" />
      <div class="row-actions">
        <Button variant="secondary" size="small" on:click={randomCreds}>Generate credentials</Button>
        <Button size="small" on:click={submitAssignment} disabled={!assignForm.service_id}>Assign</Button>
      </div>
    </div>
    <p class="hint">Leave username/password empty to let the server generate credentials. Clients use HTTP or SOCKS5 on port {PROXY_PORT}.</p>
  </section>

  <section>
    <h3>Active Assignments</h3>
    <div class="table">
      <div class="row head assign-head">
        <div>IP</div><div>Service</div><div>User</div><div>Password</div><div>Assigned</div><div>URLs</div><div>Actions</div>
      </div>
      {#each assignments as a}
        {@const urls = proxyUrls(a)}
        <div class="row assign-row">
          <div class="mono">{a.ip_address}</div>
          <div>
            <button class="link-btn" on:click={() => navigate(`/admin/services/${a.service_id}`)}>
              #{a.service_id} {a.service_name || ''}
            </button>
          </div>
          <div class="mono">
            {a.username || '—'}
            {#if a.username}
              <button class="tiny" on:click={() => copyText(a.username)}>Copy</button>
            {/if}
          </div>
          <div class="mono">
            {#if a.password}
              {revealedPasswords[a.id] ? a.password : '••••••••'}
              <button class="tiny" on:click={() => (revealedPasswords[a.id] = !revealedPasswords[a.id])}>
                {revealedPasswords[a.id] ? 'Hide' : 'Show'}
              </button>
              <button class="tiny" on:click={() => copyText(a.password)}>Copy</button>
            {:else}
              —
            {/if}
          </div>
          <div>{formatDate(a.assigned_at)}</div>
          <div class="row-actions">
            <button class="tiny" on:click={() => copyText(urls.http)}>Copy HTTP</button>
            <button class="tiny" on:click={() => copyText(urls.socks5)}>Copy SOCKS5</button>
          </div>
          <div>
            <button class="tiny danger" on:click={() => releaseAssignment(a.id)}>Release</button>
          </div>
        </div>
      {:else}
        <div class="empty-row">No active assignments.</div>
      {/each}
    </div>
  </section>

  <section>
    <div class="section-head">
      <h3>Assignment History</h3>
      <select bind:value={historyServiceFilter}>
        <option value="">All services</option>
        {#each services as service}
          <option value={service.id}>{service.id} — {service.name}</option>
        {/each}
      </select>
    </div>
    <div class="table">
      <div class="row head history-head">
        <div>Action</div><div>IP</div><div>Service</div><div>User</div><div>By</div><div>When</div>
      </div>
      {#each filteredHistory as h}
        <div class="row history-row">
          <div><span class="badge">{h.action}</span></div>
          <div class="mono">{h.ip_address}</div>
          <div>{h.service_id ?? '—'}</div>
          <div class="mono">{h.username || '—'}</div>
          <div>{h.assigned_by || '—'}</div>
          <div>{formatDate(h.created_at)}</div>
        </div>
      {:else}
        <div class="empty-row">No history.</div>
      {/each}
    </div>
  </section>
</div>

{#if showCreateSubnet}
  <div class="overlay" role="presentation" on:click|self={() => (showCreateSubnet = false)}>
    <div class="modal">
      <h3>Create Subnet</h3>
      <input bind:value={subnetForm.name} placeholder="Subnet name" />
      <input bind:value={subnetForm.cidr} placeholder="198.51.100.0/24" />
      <select bind:value={subnetForm.location_id}>
        <option value="">No location</option>
        {#each locations as loc}
          <option value={loc.id}>{loc.name}</option>
        {/each}
      </select>
      <input bind:value={subnetForm.range_start} placeholder="Range start (optional)" />
      <input bind:value={subnetForm.range_end} placeholder="Range end (optional)" />
      <select bind:value={subnetForm.allocation_strategy}>
        <option value="first_free">first_free</option>
        <option value="spread_subnets">spread_subnets</option>
        <option value="least_recently_used">least_recently_used</option>
      </select>
      <label class="field-label" for="max-resale-input">
        Resale cap (customers per IP)
        <input
          id="max-resale-input"
          type="number"
          min="1"
          bind:value={subnetForm.max_resale_count}
        />
      </label>
      <p class="hint">1 = exclusive IPs (default). Higher values let the same IP be sold to multiple customers at once.</p>
      <div class="actions">
        <Button variant="secondary" on:click={() => (showCreateSubnet = false)}>Cancel</Button>
        <Button on:click={submitSubnet} disabled={!subnetForm.name || !subnetForm.cidr}>Create</Button>
      </div>
    </div>
  </div>
{/if}

<style>
  .page { padding: 24px; display: flex; flex-direction: column; gap: 16px; }
  @media (max-width: 768px) { .page { padding: 16px; } }
  .summary {
    display: grid;
    grid-template-columns: repeat(5, minmax(0, 1fr));
    gap: 12px;
  }
  @media (max-width: 900px) {
    .summary { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  }
  .stat {
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: 10px;
    padding: 14px 16px;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .stat .label { color: var(--text-secondary); font-size: 0.85rem; }
  .stat .value { font-size: 1.4rem; font-weight: 600; color: var(--text-primary); }
  .actions { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  section {
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: 10px;
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  h3 { margin: 0; font-size: 1.05rem; }
  .section-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; }
  .form-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px;
  }
  @media (max-width: 768px) {
    .form-grid { grid-template-columns: 1fr; }
  }
  input, select {
    background: var(--bg-secondary);
    border: 1px solid var(--border-color);
    color: var(--text-primary);
    border-radius: 6px;
    padding: 8px;
    width: 100%;
  }
  .table { display: flex; flex-direction: column; gap: 0; overflow-x: auto; }
  .row {
    display: grid;
    grid-template-columns: 1.2fr 1fr 1.1fr 1fr 0.7fr 0.8fr 1.2fr;
    gap: 8px;
    padding: 10px 8px;
    border-bottom: 1px solid var(--border-color);
    align-items: center;
    font-size: 0.9rem;
  }
  .row.head { font-weight: 600; color: var(--text-secondary); border-bottom: 1px solid var(--border-color); }
  .subnet-row {
    grid-template-columns: 1fr 1.2fr 1fr 0.9fr 0.6fr 0.7fr 0.8fr 0.8fr 1.1fr;
  }
  .resale-input {
    width: 60px;
    padding: 4px 6px;
  }
  .assign-head, .assign-row {
    grid-template-columns: 1fr 1.4fr 1.2fr 1.6fr 1.1fr 1.2fr 0.7fr;
  }
  .history-head, .history-row {
    grid-template-columns: 0.8fr 1fr 0.7fr 1fr 0.9fr 1.2fr;
  }
  .empty-row { padding: 16px 8px; color: var(--text-secondary); }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; word-break: break-all; }
  .row-actions { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
  .tiny {
    width: fit-content;
    padding: 4px 8px;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background: var(--bg-secondary);
    color: var(--text-primary);
    cursor: pointer;
    font-size: 0.8rem;
  }
  .tiny.danger { color: var(--danger-color); border-color: var(--danger-color); }
  .tiny:disabled { opacity: 0.5; cursor: not-allowed; }
  .link-btn {
    background: none;
    border: none;
    color: var(--accent-color);
    cursor: pointer;
    padding: 0;
    text-align: left;
  }
  .badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 999px;
    background: var(--bg-secondary);
    border: 1px solid var(--border-color);
    font-size: 0.8rem;
  }
  .hint { margin: 0; color: var(--text-secondary); font-size: 0.85rem; }
  .field-label {
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-size: 0.85rem;
    color: var(--text-secondary);
  }
  .error { color: var(--danger-color); }
  .success { color: var(--success-color, #2e7d32); }
  .overlay {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.45);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 50;
    padding: 16px;
  }
  .modal {
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: 10px;
    padding: 20px;
    width: min(480px, 100%);
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
</style>
