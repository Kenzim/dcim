<script>
  import { onMount } from 'svelte';
  import PageHeader from '../../../PageHeader.svelte';
  import { Alert, Button, Modal, Spinner } from '../../../ui/index.js';
  import { listNonpaymentPolicies, listPolicyProducts, saveNonpaymentPolicy } from '../../../../lib/billingOpsAdminApi.js';
  import '../billingAdmin.css';
  import './opsAdmin.css';

  const DEFAULTS = [
    { scope: 'retail_default', title: 'Retail default', text: 'Applies to retail services with no product policy.' },
    { scope: 'reseller_default', title: 'Reseller default', text: 'Applies to reseller services with no product policy. Suspension stays with the reseller overdue policy, so only termination is set here.' },
  ];

  let policies = [];
  let products = [];
  let loading = true;
  let error = '';
  let success = '';
  let saving = false;

  let editing = null;
  let form = null;
  let formError = '';

  onMount(load);

  async function load() {
    loading = true;
    try {
      const [rows, productRows] = await Promise.all([
        listNonpaymentPolicies(),
        listPolicyProducts().catch(() => []),
      ]);
      policies = rows;
      products = (productRows || []).filter((row) => row.product_id);
      error = '';
    } catch (err) {
      error = err.message;
    } finally {
      loading = false;
    }
  }

  const productName = (productId) => products.find((p) => p.product_id === productId)?.name || `Product #${productId}`;
  const days = (value) => (value === null || value === undefined ? 'Never' : `${value} day${value === 1 ? '' : 's'}`);
  const defaultRow = (scope) => policies.find((row) => row.scope === scope) || null;

  $: productRows = policies.filter((row) => row.scope === 'product');
  $: usedProducts = new Set(productRows.map((row) => row.product_id));

  function startEdit(scope, row, title) {
    editing = { scope, existing: row, title };
    formError = '';
    form = {
      product_id: row?.product_id ?? '',
      suspend_after_days: row?.suspend_after_days ?? '',
      terminate_after_days: row?.terminate_after_days ?? '',
      termination_notice_days: row?.termination_notice_days ?? 7,
      unpaid_invoice_action_on_terminate: row?.unpaid_invoice_action_on_terminate ?? 'keep',
      enabled: row?.enabled ?? true,
    };
  }

  function whole(value, field, max) {
    if (value === '' || value === null || value === undefined) return null;
    const n = Number(value);
    if (!Number.isInteger(n) || n < 0 || n > max) throw new Error(`${field} must be a whole number from 0 to ${max}, or empty.`);
    return n;
  }

  async function submit() {
    formError = '';
    let body;
    try {
      const suspend = editing.scope === 'reseller_default' ? null : whole(form.suspend_after_days, 'Suspend after', 365);
      const terminate = whole(form.terminate_after_days, 'Terminate after', 730);
      const notice = whole(form.termination_notice_days, 'Notice', 90);
      if (notice === null) throw new Error('Notice days is required.');
      if (editing.scope === 'product' && !form.product_id) throw new Error('Choose a product.');
      if (suspend !== null && terminate !== null && terminate < suspend) throw new Error('Terminate after must not be before suspend after.');
      if (terminate !== null && notice > terminate) throw new Error('Notice days must not exceed terminate after.');
      body = {
        scope: editing.scope,
        suspend_after_days: suspend,
        terminate_after_days: terminate,
        termination_notice_days: notice,
        unpaid_invoice_action_on_terminate: form.unpaid_invoice_action_on_terminate,
        enabled: form.enabled,
      };
      if (editing.scope === 'product') body.product_id = Number(form.product_id);
    } catch (err) {
      formError = err.message;
      return;
    }
    saving = true;
    try {
      await saveNonpaymentPolicy(body);
      success = `${editing.title} saved.`;
      editing = null;
      await load();
    } catch (err) {
      formError = err.message;
    } finally {
      saving = false;
    }
  }
</script>

<PageHeader title="Non-payment Policies">
  <svelte:fragment slot="actions">
    <Button on:click={() => startEdit('product', null, 'Product policy')}>Add product policy</Button>
  </svelte:fragment>
</PageHeader>

<div class="ba-page">
  <p class="ba-muted ba-wide-note">
    A policy says how long after an invoice's due date an unpaid service is suspended and terminated. A product policy overrides the default for that product.
    Empty means never, which is the setting of a new installation. The suspension and termination jobs must be live as well before any policy acts.
    Saving a policy starts a new watermark for those jobs: they never act on an invoice that was already past its new trigger when you saved.
  </p>
  {#if error}<Alert type="danger">{error}</Alert>{/if}
  {#if success}<Alert type="success">{success}</Alert>{/if}

  {#if loading}
    <div class="ba-state"><Spinner /></div>
  {:else}
    <div class="ba-panel" style="margin-top:18px">
      <h3>Defaults</h3>
      <div class="ba-scroll">
        <table class="ba-table">
          <thead><tr><th>Policy</th><th>Suspend after due date</th><th>Terminate after due date</th><th>Termination notice</th><th>Unpaid invoices on termination</th><th>Enabled</th><th></th></tr></thead>
          <tbody>
            {#each DEFAULTS as item}
              {@const row = defaultRow(item.scope)}
              <tr>
                <td><strong>{item.title}</strong><div class="ba-sub">{item.text}</div></td>
                {#if row}
                  <td>{item.scope === 'reseller_default' ? 'Not set here' : days(row.suspend_after_days)}</td>
                  <td>{days(row.terminate_after_days)}</td>
                  <td>{row.termination_notice_days} days</td>
                  <td>{row.unpaid_invoice_action_on_terminate === 'cancel' ? 'Cancel them' : 'Keep them open'}</td>
                  <td><span class="ba-pill {row.enabled ? 'ok' : ''}">{row.enabled ? 'Enabled' : 'Disabled'}</span></td>
                {:else}
                  <td colspan="5"><span class="ba-muted">No policy saved yet; nothing is suspended or terminated.</span></td>
                {/if}
                <td><button class="ba-link" on:click={() => startEdit(item.scope, row, item.title)}>Edit</button></td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    </div>

    <div class="ba-panel">
      <h3>Product policies <small>{productRows.length}</small></h3>
      {#if productRows.length === 0}
        <p class="ba-empty">No product has its own policy.</p>
      {:else}
        <div class="ba-scroll">
          <table class="ba-table">
            <thead><tr><th>Product</th><th>Suspend after due date</th><th>Terminate after due date</th><th>Termination notice</th><th>Unpaid invoices on termination</th><th>Enabled</th><th></th></tr></thead>
            <tbody>
              {#each productRows as row (row.id)}
                <tr>
                  <td>{productName(row.product_id)}</td>
                  <td>{days(row.suspend_after_days)}</td>
                  <td>{days(row.terminate_after_days)}</td>
                  <td>{row.termination_notice_days} days</td>
                  <td>{row.unpaid_invoice_action_on_terminate === 'cancel' ? 'Cancel them' : 'Keep them open'}</td>
                  <td><span class="ba-pill {row.enabled ? 'ok' : ''}">{row.enabled ? 'Enabled' : 'Disabled'}</span></td>
                  <td><button class="ba-link" on:click={() => startEdit('product', row, `Policy for ${productName(row.product_id)}`)}>Edit</button></td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    </div>
  {/if}
</div>

{#if editing}
  <Modal title={editing.title} onClose={() => (editing = null)}>
    <form class="ba-form" on:submit|preventDefault={submit}>
      {#if editing.scope === 'product'}
        <label>Product
          <select bind:value={form.product_id} disabled={!!editing.existing}>
            <option value="">Choose a product</option>
            {#each products.filter((p) => editing.existing || !usedProducts.has(p.product_id)) as product (product.product_id)}
              <option value={product.product_id}>{product.name}</option>
            {/each}
          </select>
        </label>
      {/if}
      {#if editing.scope === 'reseller_default'}
        <p class="ba-hint">Reseller suspension is set by the reseller overdue policy, so there is no suspension setting here.</p>
      {:else}
        <label>Suspend after (days past the due date)
          <input type="number" min="0" max="365" step="1" bind:value={form.suspend_after_days} placeholder="Empty: never suspend" />
        </label>
      {/if}
      <label>Terminate after (days past the due date)
        <input type="number" min="0" max="730" step="1" bind:value={form.terminate_after_days} placeholder="Empty: never terminate" />
      </label>
      <label>Termination notice (days before termination)
        <input type="number" min="0" max="90" step="1" bind:value={form.termination_notice_days} />
      </label>
      <label>When a service is terminated, its unpaid invoices are
        <select bind:value={form.unpaid_invoice_action_on_terminate}>
          <option value="keep">Kept open (the debt stays)</option>
          <option value="cancel">Cancelled</option>
        </select>
      </label>
      <label class="ba-check"><input type="checkbox" bind:checked={form.enabled} /> Enabled</label>
      {#if form.terminate_after_days !== '' && form.terminate_after_days !== null}
        <Alert type="warning">Services will be terminated when this policy applies and the termination job is live. Termination is permanent.</Alert>
      {/if}
      {#if formError}<div class="ba-error">{formError}</div>{/if}
    </form>
    <svelte:fragment slot="footer">
      <Button variant="secondary" on:click={() => (editing = null)}>Cancel</Button>
      <Button on:click={submit} disabled={saving}>Save policy</Button>
    </svelte:fragment>
  </Modal>
{/if}
