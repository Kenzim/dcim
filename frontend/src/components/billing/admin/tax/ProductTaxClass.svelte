<script>
  import { onMount } from 'svelte';
  import { Alert, Button } from '../../../ui/index.js';
  import { listTaxClasses, setProductTaxClass } from '../../../../lib/billingTaxAdminApi.js';

  export let storeProductId;
  export let taxClassId = null;
  export let onChanged = () => {};

  let classes = [];
  let choice = taxClassId ? String(taxClassId) : '';
  let saving = false;
  let error = '';
  let success = '';

  onMount(async () => {
    try {
      classes = await listTaxClasses();
    } catch (err) {
      error = err.message;
    }
  });

  async function save() {
    saving = true;
    error = '';
    success = '';
    try {
      await setProductTaxClass(storeProductId, choice ? Number(choice) : null);
      success = 'Tax class saved. It applies from the next calculation; published invoices keep their tax.';
      onChanged();
    } catch (err) {
      error = err.message;
    } finally {
      saving = false;
    }
  }

  $: changed = (choice ? Number(choice) : null) !== (taxClassId ?? null);
</script>

<div class="tax-class">
  <label>Tax class
    <select bind:value={choice}>
      <option value="">Standard (default)</option>
      {#each classes as c (c.id)}<option value={String(c.id)}>{c.name} ({c.code})</option>{/each}
    </select>
  </label>
  <Button size="small" on:click={save} disabled={saving || !changed}>Save tax class</Button>
</div>
{#if error}<Alert type="danger">{error}</Alert>{/if}
{#if success}<Alert type="success">{success}</Alert>{/if}

<style>
  .tax-class { display: flex; gap: 10px; align-items: flex-end; flex-wrap: wrap; margin: 0 0 16px; }
  .tax-class label { display: grid; gap: 4px; font-size: 13px; font-weight: 600; }
  .tax-class select { padding: 8px; border: 1px solid var(--border-color); border-radius: 8px; background: var(--bg-primary); color: var(--text-primary); font-weight: 400; }
</style>
