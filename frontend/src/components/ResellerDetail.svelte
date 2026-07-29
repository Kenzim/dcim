<script>
  import { onMount } from 'svelte';
  import PageHeader from './PageHeader.svelte';
  import { Button, Modal, FormGroup, FormError } from './ui/index.js';
  import {
    adjustResellerCredit,
    createResellerQuota,
    deleteResellerProductAccess,
    deleteResellerQuota,
    getReseller,
    getServerGroups,
    listProxmoxClusters,
    listResellerBasePrices,
    listResellerGroupPrices,
    listResellerGroups,
    listResellerInvoices,
    listResellerLedger,
    listResellerProductAccess,
    listResellerQuotas,
    rotateResellerKey,
    setResellerProductAccess,
    updateReseller,
    updateResellerQuota,
  } from '../lib/api.js';

  export let resellerId;

  let reseller = null;
  let groups = [];
  let access = [];
  let quotas = [];
  let invoices = [];
  let ledger = [];
  let prices = [];
  let serverGroups = [];
  let clusters = [];
  let loading = true;
  let error = '';
  let busy = false;
  let profileForm = {};
  let creditForm = { amount_cents: '', reason: '' };
  let quotaForm = emptyQuota();
  let editingQuotaId = null;
  let quotaError = '';
  let keyReveal = { open: false, apiKey: '', copied: false };

  onMount(load);

  function emptyQuota() {
    return {
      scope_type: 'product',
      scope_id: '',
      max_services: '',
      max_cpu_cores: '',
      max_ram_mb: '',
      max_disk_gb: '',
      enabled: true,
    };
  }

  async function load() {
    loading = true;
    error = '';
    try {
      const [detail, groupRows, basePrices, serverGroupRows, clusterRows] = await Promise.all([
        getReseller(resellerId),
        listResellerGroups(),
        listResellerBasePrices(),
        getServerGroups(),
        listProxmoxClusters(),
      ]);
      reseller = detail;
      groups = groupRows;
      serverGroups = serverGroupRows;
      clusters = clusterRows;
      profileForm = {
        username: reseller.user.username,
        email: reseller.user.email,
        group_id: reseller.group_id || '',
        status: reseller.status,
        charge_preference: reseller.charge_preference,
        nonpayment_policy: reseller.nonpayment_policy,
        billing_hold: reseller.billing_hold,
        billing_hold_reason: reseller.billing_hold_reason || '',
      };
      prices = reseller.group_id
        ? await listResellerGroupPrices(reseller.group_id)
        : basePrices.map((row) => ({
            ...row,
            effective_setup_cents: row.setup_cents,
            effective_monthly_cents: row.monthly_cents,
          }));
      [access, quotas, invoices, ledger] = await Promise.all([
        listResellerProductAccess(resellerId),
        listResellerQuotas({ reseller_id: resellerId }),
        listResellerInvoices({ reseller_id: resellerId, limit: 25 }),
        listResellerLedger(resellerId, { limit: 25 }),
      ]);
    } catch (err) {
      error = err.message || String(err);
    } finally {
      loading = false;
    }
  }

  async function saveProfile() {
    busy = true;
    error = '';
    try {
      await updateReseller(resellerId, {
        username: profileForm.username.trim(),
        email: profileForm.email.trim(),
        group_id: profileForm.group_id ? Number(profileForm.group_id) : null,
        status: profileForm.status,
        charge_preference: profileForm.charge_preference,
        nonpayment_policy: profileForm.nonpayment_policy,
        billing_hold: profileForm.billing_hold,
        billing_hold_reason: profileForm.billing_hold
          ? profileForm.billing_hold_reason.trim()
          : null,
      });
      await load();
    } catch (err) {
      error = err.message || String(err);
    } finally {
      busy = false;
    }
  }

  async function rotateKey() {
    if (!confirm('Rotate this API key? The current key will stop working immediately.')) return;
    busy = true;
    try {
      const result = await rotateResellerKey(resellerId);
      keyReveal = { open: true, apiKey: result.api_key, copied: false };
      await load();
    } catch (err) {
      error = err.message || String(err);
    } finally {
      busy = false;
    }
  }

  async function copyKey() {
    try {
      await navigator.clipboard.writeText(keyReveal.apiKey);
      keyReveal = { ...keyReveal, copied: true };
    } catch (_) {
      keyReveal = { ...keyReveal, copied: false };
    }
  }

  async function submitCredit() {
    busy = true;
    error = '';
    try {
      const amount = Number(creditForm.amount_cents);
      if (!Number.isInteger(amount) || amount === 0) throw new Error('Enter a signed, nonzero integer number of cents');
      await adjustResellerCredit(resellerId, {
        amount_cents: amount,
        reason: creditForm.reason.trim(),
      });
      creditForm = { amount_cents: '', reason: '' };
      await load();
    } catch (err) {
      const detail = err.detail;
      error = detail?.code === 'insufficient_credit'
        ? `Insufficient credit: ${detail.available_cents} cents available`
        : (err.message || String(err));
    } finally {
      busy = false;
    }
  }

  async function changeAccess(row, value) {
    try {
      if (value === 'inherit') {
        if (row.direct_rule) await deleteResellerProductAccess(resellerId, row.product_id);
      } else {
        await setResellerProductAccess(resellerId, row.product_id, value === 'allow');
      }
      access = await listResellerProductAccess(resellerId);
    } catch (err) {
      error = err.message || String(err);
    }
  }

  function startQuotaEdit(quota) {
    editingQuotaId = quota.id;
    quotaForm = {
      scope_type: quota.scope_type,
      scope_id: String(quota.scope_id),
      max_services: quota.max_services ?? '',
      max_cpu_cores: quota.max_cpu_cores ?? '',
      max_ram_mb: quota.max_ram_mb ?? '',
      max_disk_gb: quota.max_disk_gb ?? '',
      enabled: quota.enabled,
    };
    quotaError = '';
  }

  function cancelQuotaEdit() {
    editingQuotaId = null;
    quotaForm = emptyQuota();
    quotaError = '';
  }

  function nullableInteger(value) {
    if (value === '' || value === null || value === undefined) return null;
    const parsed = Number(value);
    if (!Number.isInteger(parsed) || parsed < 0) throw new Error('Quota limits must be nonnegative integers');
    return parsed;
  }

  async function saveQuota() {
    quotaError = '';
    busy = true;
    try {
      const limits = {
        max_services: nullableInteger(quotaForm.max_services),
        max_cpu_cores: nullableInteger(quotaForm.max_cpu_cores),
        max_ram_mb: nullableInteger(quotaForm.max_ram_mb),
        max_disk_gb: nullableInteger(quotaForm.max_disk_gb),
        enabled: quotaForm.enabled,
      };
      if (editingQuotaId) {
        await updateResellerQuota(editingQuotaId, limits);
      } else {
        await createResellerQuota({
          reseller_id: Number(resellerId),
          scope_type: quotaForm.scope_type,
          scope_id: Number(quotaForm.scope_id),
          ...limits,
        });
      }
      cancelQuotaEdit();
      quotas = await listResellerQuotas({ reseller_id: resellerId });
    } catch (err) {
      quotaError = err.message || String(err);
    } finally {
      busy = false;
    }
  }

  async function removeQuota(quota) {
    if (!confirm(`Delete quota for ${quota.scope_name}?`)) return;
    try {
      await deleteResellerQuota(quota.id);
      quotas = await listResellerQuotas({ reseller_id: resellerId });
    } catch (err) {
      error = err.message || String(err);
    }
  }

  function scopeTargets() {
    if (quotaForm.scope_type === 'product') {
      return prices.map((row) => ({ id: row.product_id, name: row.product.name }));
    }
    if (quotaForm.scope_type === 'server_group') return serverGroups;
    return clusters;
  }

  function priceFor(productId) {
    return prices.find((row) => row.product_id === productId);
  }

  function money(cents) {
    if (cents === null || cents === undefined) return 'Not configured';
    return new Intl.NumberFormat(undefined, { style: 'currency', currency: 'USD' }).format(cents / 100);
  }

  function dateTime(value) {
    return value ? new Date(value).toLocaleString() : '—';
  }
</script>

<PageHeader title={reseller ? `Reseller: ${reseller.user.username}` : 'Reseller'} />

<div class="page">
  <a class="back" href="/admin/resellers">← Back to resellers</a>
  {#if error}<div class="alert alert-danger">{error}</div>{/if}

  {#if loading}
    <div class="state">Loading reseller…</div>
  {:else if reseller}
    <div class="stats">
      <div class="stat"><span>Balance</span><strong>{money(reseller.balance_cents)}</strong></div>
      <div class="stat"><span>Services</span><strong>{reseller.service_summary.total}</strong></div>
      <div class="stat"><span>Invoices</span><strong>{reseller.invoice_summary.total}</strong></div>
      <div class="stat"><span>Billing hold</span><strong>{reseller.billing_hold ? (reseller.billing_hold_reason || 'Held') : 'Clear'}</strong></div>
      <div class="stat"><span>API prefix</span><strong><code>{reseller.api_key_prefix || 'Not issued'}</code></strong></div>
    </div>

    <section class="panel">
      <div class="panel-title"><h2>Profile and access</h2><Button variant="secondary" size="small" disabled={busy} on:click={rotateKey}>Rotate API key</Button></div>
      <form class="form-grid" on:submit|preventDefault={saveProfile}>
        <label>Username<input bind:value={profileForm.username} required /></label>
        <label>Email<input type="email" bind:value={profileForm.email} required /></label>
        <label>Group
          <select bind:value={profileForm.group_id}>
            <option value="">Ungrouped</option>
            {#each groups as group}<option value={group.id}>{group.name}{group.enabled ? '' : ' (disabled)'}</option>{/each}
          </select>
        </label>
        <label>Status
          <select bind:value={profileForm.status}>
            <option value="active">Active</option>
            <option value="suspended">Suspended</option>
            <option value="disabled">Disabled</option>
          </select>
        </label>
        <label>Charge preference
          <select bind:value={profileForm.charge_preference}>
            <option value="credit_first">Credit first</option>
            <option value="payment_first">Payment first</option>
          </select>
        </label>
        <label>Nonpayment policy
          <select bind:value={profileForm.nonpayment_policy}>
            <option value="block_new">Block new deployments</option>
            <option value="suspend_all">Suspend all services</option>
          </select>
        </label>
        <label class="check"><input type="checkbox" bind:checked={profileForm.billing_hold} /> Billing hold</label>
        <label>Billing hold reason
          <input
            bind:value={profileForm.billing_hold_reason}
            maxlength="512"
            required={profileForm.billing_hold}
            disabled={!profileForm.billing_hold}
            placeholder="Required while held"
          />
        </label>
        <div class="form-action"><Button type="submit" disabled={busy}>Save profile</Button></div>
      </form>
    </section>

    <section class="panel">
      <h2>Manual credit adjustment</h2>
      <form class="credit-form" on:submit|preventDefault={submitCredit}>
        <label>Signed amount (cents)<input type="number" step="1" bind:value={creditForm.amount_cents} placeholder="5000 or -5000" required /></label>
        <label class="reason">Reason<input bind:value={creditForm.reason} maxlength="512" required placeholder="Required audit reason" /></label>
        <Button type="submit" disabled={busy}>Apply adjustment</Button>
      </form>
      <p class="hint">Negative adjustments cannot take the reseller below zero. Every change records the admin and reason in the ledger.</p>
    </section>

    <section class="panel">
      <h2>Product access and effective prices</h2>
      <p class="hint">Direct rules override the reseller group. No direct or group rule means explicit default-deny.</p>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Product</th><th>Setup</th><th>Monthly</th><th>Group rule</th><th>Direct rule</th><th>Effective</th></tr></thead>
          <tbody>
            {#each access as row}
              <tr>
                <td><strong>{row.product.name}</strong><div class="muted">{row.product.code}</div></td>
                <td>{money(priceFor(row.product_id)?.effective_setup_cents)}</td>
                <td>{money(priceFor(row.product_id)?.effective_monthly_cents)}</td>
                <td>{row.group_rule ? (row.group_rule.allowed ? 'Allow' : 'Deny') : 'None'}</td>
                <td>
                  <select value={row.direct_rule ? (row.direct_rule.allowed ? 'allow' : 'deny') : 'inherit'} on:change={(event) => changeAccess(row, event.currentTarget.value)}>
                    <option value="inherit">Inherit</option>
                    <option value="allow">Allow</option>
                    <option value="deny">Deny</option>
                  </select>
                </td>
                <td><span class:allowed={row.effective_allowed} class:denied={!row.effective_allowed}>{row.effective_allowed ? 'Allowed' : 'Denied'} · {row.effective_source.replace('_', ' ')}</span></td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    </section>

    <section class="panel">
      <h2>Stock quotas and usage</h2>
      {#if quotaError}<FormError>{quotaError}</FormError>{/if}
      <div class="quota-form">
        <label>Scope
          <select bind:value={quotaForm.scope_type} disabled={!!editingQuotaId} on:change={() => (quotaForm.scope_id = '')}>
            <option value="product">Product</option>
            <option value="server_group">Server group</option>
            <option value="proxmox_cluster">Proxmox cluster</option>
          </select>
        </label>
        <label>Target
          <select bind:value={quotaForm.scope_id} disabled={!!editingQuotaId} required>
            <option value="">Choose target</option>
            {#each scopeTargets() as target}<option value={target.id}>{target.name}</option>{/each}
          </select>
        </label>
        <label>Services<input type="number" min="0" step="1" bind:value={quotaForm.max_services} /></label>
        <label>CPU cores<input type="number" min="0" step="1" bind:value={quotaForm.max_cpu_cores} /></label>
        <label>RAM MB<input type="number" min="0" step="1" bind:value={quotaForm.max_ram_mb} /></label>
        <label>Disk GB<input type="number" min="0" step="1" bind:value={quotaForm.max_disk_gb} /></label>
        <label class="check"><input type="checkbox" bind:checked={quotaForm.enabled} /> Enabled</label>
        <div class="quota-actions"><Button size="small" disabled={busy || !quotaForm.scope_id} on:click={saveQuota}>{editingQuotaId ? 'Update quota' : 'Add quota'}</Button>{#if editingQuotaId}<Button size="small" variant="secondary" on:click={cancelQuotaEdit}>Cancel</Button>{/if}</div>
      </div>
      <div class="quota-list">
        {#each quotas as quota}
          <article class:shadowed={!quota.is_effective}>
            <div><strong>{quota.scope_name}</strong><div class="muted">{quota.scope_type.replace('_', ' ')} · {quota.source}{quota.is_effective ? '' : ' · shadowed'}</div></div>
            <div class="usage">Services {quota.usage.services}/{quota.max_services ?? '∞'} · CPU {quota.usage.cpu_cores}/{quota.max_cpu_cores ?? '∞'} · RAM {quota.usage.ram_mb}/{quota.max_ram_mb ?? '∞'} MB · Disk {quota.usage.disk_gb}/{quota.max_disk_gb ?? '∞'} GB</div>
            {#if quota.source === 'direct'}
              <div class="row-actions"><Button size="small" variant="secondary" on:click={() => startQuotaEdit(quota)}>Edit</Button><Button size="small" variant="danger" on:click={() => removeQuota(quota)}>Delete</Button></div>
            {/if}
          </article>
        {:else}
          <p class="hint">No direct or group quota is configured. Provisioning remains default-deny without a matching quota.</p>
        {/each}
      </div>
    </section>

    <div class="two-column">
      <section class="panel">
        <div class="panel-title">
          <h2>Recent invoices</h2>
          <a class="muted" href="/admin/billing?reseller_id={resellerId}">Open billing panel</a>
        </div>
        {#each invoices as invoice}
          <a class="finance-row link-row" href="/admin/billing?invoice_id={invoice.id}">
            <div><strong>#{invoice.invoice_number}</strong><div class="muted">{invoice.purpose.replace('_', ' ')}</div></div>
            <div class="right"><strong>{money(invoice.amount_cents)}</strong><div class="muted">{invoice.status} · {dateTime(invoice.created_at)}</div></div>
          </a>
        {:else}<p class="hint">No invoices.</p>{/each}
      </section>
      <section class="panel">
        <h2>Recent credit ledger</h2>
        {#each ledger as entry}
          <div class="finance-row"><div><strong class:positive={entry.amount_cents > 0} class:negative={entry.amount_cents < 0}>{entry.amount_cents > 0 ? '+' : ''}{money(entry.amount_cents)}</strong><div class="muted">{entry.description || entry.entry_type}</div></div><div class="right"><strong>{money(entry.balance_after_cents)}</strong><div class="muted">{dateTime(entry.created_at)}</div></div></div>
        {:else}<p class="hint">No ledger entries.</p>{/each}
      </section>
    </div>
  {/if}
</div>

{#if keyReveal.open}
  <Modal title="Copy the new API key now" onClose={() => (keyReveal.open = false)}>
    <div class="key-warning">This plaintext key is shown exactly once. Store it securely before closing this panel; the previous key is already invalid.</div>
    <code class="key-value">{keyReveal.apiKey}</code>
    <svelte:fragment slot="footer">
      <Button variant="secondary" on:click={copyKey}>{keyReveal.copied ? 'Copied' : 'Copy key'}</Button>
      <Button on:click={() => (keyReveal.open = false)}>I stored it</Button>
    </svelte:fragment>
  </Modal>
{/if}

<style>
  .page { padding: 32px; color: var(--text-primary); }
  .back { display: inline-block; margin-bottom: 18px; color: var(--accent-color); text-decoration: none; font-weight: 600; }
  .stats { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; margin-bottom: 18px; }
  .stat, .panel { background: var(--bg-primary); border: 1px solid var(--border-color); border-radius: 12px; padding: 18px; }
  .stat span { display: block; color: var(--text-secondary); font-size: 12px; margin-bottom: 6px; }
  .stat strong { font-size: 20px; }
  .panel { margin-bottom: 18px; }
  .panel h2 { margin: 0 0 14px; font-size: 18px; }
  .panel-title { display: flex; justify-content: space-between; gap: 12px; align-items: center; }
  .form-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; align-items: end; }
  label { display: flex; flex-direction: column; gap: 5px; color: var(--text-secondary); font-size: 12px; font-weight: 700; }
  input, select {
    min-width: 0;
    padding: 9px 10px;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    color: var(--text-primary);
    background-color: var(--bg-secondary);
  }
  .form-action { display: flex; align-items: end; }
  .credit-form { display: flex; flex-wrap: wrap; gap: 12px; align-items: end; }
  .credit-form .reason { flex: 1; min-width: 260px; }
  .hint, .muted { color: var(--text-secondary); font-size: 12px; }
  .table-wrap { overflow-x: auto; border: 1px solid var(--border-color); border-radius: 9px; }
  table { width: 100%; min-width: 850px; border-collapse: collapse; }
  th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid var(--border-color); }
  th { background: var(--bg-tertiary); color: var(--text-secondary); font-size: 11px; text-transform: uppercase; }
  tbody tr:last-child td { border-bottom: 0; }
  .allowed, .positive { color: var(--success-color); }
  .denied, .negative { color: var(--danger-color); }
  .quota-form { display: grid; grid-template-columns: repeat(4, minmax(120px, 1fr)); gap: 10px; align-items: end; padding: 14px; background: var(--bg-tertiary); border-radius: 9px; }
  .check { flex-direction: row; align-items: center; padding-bottom: 10px; }
  .quota-actions, .row-actions { display: flex; gap: 7px; }
  .quota-list { margin-top: 12px; display: grid; gap: 8px; }
  .quota-list article { display: grid; grid-template-columns: minmax(150px, .8fr) minmax(280px, 2fr) auto; gap: 12px; align-items: center; padding: 12px; border: 1px solid var(--border-color); border-radius: 9px; }
  .quota-list article.shadowed { opacity: .65; }
  .usage { font-size: 12px; color: var(--text-secondary); }
  .two-column { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
  .finance-row { display: flex; justify-content: space-between; gap: 12px; padding: 10px 0; border-bottom: 1px solid var(--border-color); }
  .finance-row:last-child { border-bottom: 0; }
  a.finance-row.link-row { color: inherit; text-decoration: none; }
  a.finance-row.link-row:hover { background: var(--bg-tertiary); border-radius: 8px; padding-left: 8px; padding-right: 8px; }
  .panel-title a { color: var(--accent-color); text-decoration: none; font-size: 12px; font-weight: 600; }
  .right { text-align: right; }
  .state { padding: 48px; text-align: center; color: var(--text-secondary); }
  .key-warning { padding: 12px; margin-bottom: 16px; border-radius: 8px; color: var(--warning-text); background: var(--warning-bg); }
  .key-value { display: block; padding: 14px; border-radius: 8px; background: var(--bg-tertiary); overflow-wrap: anywhere; user-select: all; }
  @media (max-width: 1000px) {
    .stats, .form-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .quota-form { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .two-column { grid-template-columns: 1fr; }
  }
  @media (max-width: 768px) {
    .page { padding: 16px; }
    .stats, .form-grid, .quota-form { grid-template-columns: 1fr; }
    .quota-list article { grid-template-columns: 1fr; }
  }
</style>
