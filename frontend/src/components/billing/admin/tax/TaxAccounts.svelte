<script>
  import { onMount } from 'svelte';
  import { Alert, Button, Spinner } from '../../../ui/index.js';
  import { searchCommerceBillingAccounts } from '../../../../lib/billingTaxAdminApi.js';
  import AccountTaxPanel from './AccountTaxPanel.svelte';
  import { label } from './taxUi.js';

  export let accountId = '';

  let accounts = [];
  let search = '';
  let loading = false;
  let error = '';
  let selected = accountId ? String(accountId) : '';
  let typed = '';

  onMount(() => {
    if (!selected) find();
  });

  async function find() {
    loading = true;
    error = '';
    try {
      accounts = await searchCommerceBillingAccounts({ limit: 50, ...(search.trim() ? { q: search.trim() } : {}) });
    } catch (err) {
      error = err.message;
    } finally {
      loading = false;
    }
  }
</script>

{#if selected}
  <div class="ba-actions" style="margin-top:0">
    <Button variant="secondary" size="small" on:click={() => { selected = ''; find(); }}>Back to the account list</Button>
  </div>
  {#key selected}
    <AccountTaxPanel billingAccountId={Number(selected)} />
  {/key}
{:else}
  <div class="ba-panel">
    <h3>Accounts</h3>
    <p class="ba-muted">Choose a billing account to see and change its tax settings: the exempt flag, exemptions and tax ID.</p>
    {#if error}<Alert type="danger">{error}</Alert>{/if}
    <div class="ba-toolbar">
      <label>Search<input bind:value={search} placeholder="Username or email" on:change={find} /></label>
      <Button variant="secondary" on:click={find}>Search</Button>
      <label>Account number<input bind:value={typed} inputmode="numeric" style="width:120px" /></label>
      <Button variant="secondary" disabled={!/^[1-9]\d*$/.test(typed.trim())} on:click={() => (selected = typed.trim())}>Open account</Button>
    </div>
    {#if loading}
      <div class="ba-state"><Spinner /></div>
    {:else if accounts.length === 0}
      <p class="ba-empty">No billing accounts.</p>
    {:else}
      <div class="ba-scroll">
        <table class="ba-table">
          <thead><tr><th>#</th><th>Owner</th><th>Type</th><th>Status</th><th>Tax</th><th></th></tr></thead>
          <tbody>
            {#each accounts as account (account.id)}
              <tr>
                <td>{account.id}</td>
                <td>{account.reseller_name || account.username || '-'}<div class="ba-hint">{account.email || ''}</div></td>
                <td>{label(account.account_type)}</td>
                <td>{label(account.status)}</td>
                <td>{#if account.tax_exempt}<span class="ba-pill info">exempt</span>{:else}-{/if}</td>
                <td><button class="ba-link" on:click={() => (selected = String(account.id))}>Open</button></td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  </div>
{/if}
