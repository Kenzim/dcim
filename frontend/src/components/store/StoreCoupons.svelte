<script>
  import { onMount } from 'svelte';
  import PageHeader from '../PageHeader.svelte';
  import { Alert, Button, Modal, Spinner } from '../ui/index.js';
  import { adminStoreCreateCoupon, adminStoreListCoupons, adminStoreUpdateCoupon } from '../../lib/api.js';
  import { formatMoney } from '../../lib/resellerMoney.js';

  let coupons = [];
  let loading = true;
  let saving = false;
  let error = '';
  let success = '';
  let editing = null;
  let form = blankForm();

  function blankForm() {
    return {
      code: '',
      percent_off: '',
      amount_off_cents: '',
      currency: 'USD',
      applies_to: 'all',
      duration: 'once',
      max_uses: '',
      enabled: true,
    };
  }

  onMount(load);

  async function load() {
    loading = true;
    error = '';
    try {
      coupons = await adminStoreListCoupons();
    } catch (err) {
      error = err.message || 'Failed to load coupons';
      coupons = [];
    } finally {
      loading = false;
    }
  }

  function openCreate() {
    editing = 'new';
    form = blankForm();
  }

  function openEdit(row) {
    editing = row.id;
    form = {
      code: row.code,
      percent_off: row.percent_off ?? '',
      amount_off_cents: row.amount_off_cents ? row.amount_off_cents / 100 : '',
      currency: row.currency || 'USD',
      applies_to: row.applies_to,
      duration: row.duration,
      max_uses: row.max_uses ?? '',
      enabled: row.enabled,
    };
  }

  function buildPayload() {
    const payload = {
      code: form.code.trim(),
      currency: form.currency.trim().toUpperCase(),
      applies_to: form.applies_to,
      duration: form.duration,
      enabled: form.enabled,
    };
    if (form.percent_off !== '') payload.percent_off = Number(form.percent_off);
    if (form.amount_off_cents !== '') payload.amount_off_cents = Math.round(Number(form.amount_off_cents) * 100);
    if (form.max_uses !== '') payload.max_uses = Number(form.max_uses);
    return payload;
  }

  async function save() {
    saving = true;
    error = '';
    success = '';
    try {
      const payload = buildPayload();
      if (editing === 'new') {
        await adminStoreCreateCoupon(payload);
        success = 'Coupon created.';
      } else {
        await adminStoreUpdateCoupon(editing, payload);
        success = 'Coupon updated.';
      }
      editing = null;
      await load();
    } catch (err) {
      error = err.message || 'Save failed';
    } finally {
      saving = false;
    }
  }

  function discountLabel(row) {
    if (row.percent_off) return `${row.percent_off}% off`;
    if (row.amount_off_cents) return formatMoney(row.amount_off_cents, row.currency);
    return '—';
  }
</script>

<PageHeader title="Store Coupons">
  <svelte:fragment slot="actions">
      <Button on:click={openCreate}>Add coupon</Button>
    </svelte:fragment>
</PageHeader>

<div class="page">
  {#if error}<Alert type="danger">{error}</Alert>{/if}
  {#if success}<Alert type="success">{success}</Alert>{/if}

  {#if loading}
    <div class="state"><Spinner /><span>Loading…</span></div>
  {:else if coupons.length === 0}
    <div class="empty">
      <p>No coupons configured.</p>
      <Button on:click={openCreate}>Create a coupon</Button>
    </div>
  {:else}
    <div class="panel">
      <table>
        <thead>
          <tr><th>Code</th><th>Discount</th><th>Uses</th><th>Duration</th><th>Status</th><th></th></tr>
        </thead>
        <tbody>
          {#each coupons as row (row.id)}
            <tr>
              <td><code>{row.code}</code></td>
              <td>{discountLabel(row)}</td>
              <td>{row.used_count}{row.max_uses ? ` / ${row.max_uses}` : ''}</td>
              <td>{row.duration}</td>
              <td>{row.enabled ? 'Enabled' : 'Disabled'}</td>
              <td><Button variant="secondary" on:click={() => openEdit(row)}>Edit</Button></td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</div>

{#if editing}
  <Modal title={editing === 'new' ? 'New coupon' : 'Edit coupon'} onClose={() => (editing = null)}>
    <label>Code <input bind:value={form.code} required /></label>
    <div class="row">
      <label>Percent off <input type="number" min="1" max="100" bind:value={form.percent_off} placeholder="e.g. 10" /></label>
      <label>Amount off (USD) <input type="number" step="0.01" bind:value={form.amount_off_cents} placeholder="e.g. 5.00" /></label>
    </div>
    <label>Applies to
      <select bind:value={form.applies_to}>
        <option value="all">All products</option>
        <option value="product">Single product</option>
        <option value="category">Category</option>
      </select>
    </label>
    <label>Duration
      <select bind:value={form.duration}>
        <option value="once">Once</option>
        <option value="repeating">Repeating</option>
        <option value="forever">Forever</option>
      </select>
    </label>
    <label>Max uses <input type="number" bind:value={form.max_uses} placeholder="Unlimited" /></label>
    <label class="check"><input type="checkbox" bind:checked={form.enabled} /> Enabled</label>
    <svelte:fragment slot="footer">
      <Button variant="secondary" on:click={() => (editing = null)}>Cancel</Button>
      <Button on:click={save} disabled={saving}>{saving ? 'Saving…' : 'Save'}</Button>
    </svelte:fragment>
  </Modal>
{/if}

<style>
  .page { padding: 28px 32px 36px; }
  .state { min-height: 200px; display: grid; place-items: center; gap: 12px; }
  .panel { border: 1px solid var(--border-color); border-radius: var(--radius-lg); background: var(--bg-primary); overflow: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th, td { padding: 12px 14px; text-align: left; border-bottom: 1px solid var(--border-color); }
  th { font-size: 12px; text-transform: uppercase; color: var(--text-tertiary); }
  .empty { text-align: center; padding: 48px 24px; border: 1px dashed var(--border-color); border-radius: var(--radius-lg); }
  .empty p { color: var(--text-secondary); margin-bottom: 16px; }
  label { display: grid; gap: 6px; margin-bottom: 14px; font-size: 13px; font-weight: 600; }
  input, select { padding: 9px 10px; border: 1px solid var(--border-color); border-radius: 8px; background: var(--bg-primary); color: var(--text-primary); }
  .row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .check { display: flex; align-items: center; gap: 8px; font-weight: 500; }
</style>
