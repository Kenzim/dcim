<script>
  import { onMount } from 'svelte';
  import PageHeader from '../../PageHeader.svelte';
  import { Tabs } from '../../ui/index.js';
  import TaxOverview from './tax/TaxOverview.svelte';
  import TaxClasses from './tax/TaxClasses.svelte';
  import TaxRules from './tax/TaxRules.svelte';
  import TaxRegistrations from './tax/TaxRegistrations.svelte';
  import TaxExemptions from './tax/TaxExemptions.svelte';
  import TaxAccounts from './tax/TaxAccounts.svelte';
  import TaxPacks from './tax/TaxPacks.svelte';
  import TaxLegalNotes from './tax/TaxLegalNotes.svelte';
  import TaxIdChecks from './tax/TaxIdChecks.svelte';
  import TaxCalculator from './tax/TaxCalculator.svelte';
  import TaxExchangeRates from './tax/TaxExchangeRates.svelte';
  import './billingAdmin.css';

  const TABS = [
    { id: 'overview', label: 'Overview' },
    { id: 'classes', label: 'Classes' },
    { id: 'rules', label: 'Rules' },
    { id: 'registrations', label: 'Registrations' },
    { id: 'exemptions', label: 'Exemptions' },
    { id: 'accounts', label: 'Accounts' },
    { id: 'packs', label: 'Packs' },
    { id: 'notes', label: 'Legal notes' },
    { id: 'idchecks', label: 'Tax ID checks' },
    { id: 'calculator', label: 'Calculator' },
    { id: 'rates', label: 'Exchange rates' },
  ];

  let tab = 'overview';
  let accountId = '';

  onMount(() => {
    const params = new URLSearchParams(window.location.search);
    const wanted = params.get('tab');
    if (wanted && TABS.some((t) => t.id === wanted)) tab = wanted;
    if (/^[1-9]\d*$/.test(params.get('account') || '')) {
      accountId = params.get('account');
      tab = 'accounts';
    }
  });

  function openTab(id) {
    tab = id;
    const url = new URL(window.location.href);
    url.searchParams.set('tab', id);
    url.searchParams.delete('account');
    window.history.replaceState(window.history.state, '', url);
  }
</script>

<PageHeader title="Tax" />

<div class="ba-page">
  <div class="ba-tabs-gap">
    <Tabs tabs={TABS} active={tab} on:change={(event) => openTab(event.detail.id)} />
  </div>
  {#if tab === 'overview'}
    <TaxOverview onOpenTab={openTab} />
  {:else if tab === 'classes'}
    <TaxClasses />
  {:else if tab === 'rules'}
    <TaxRules />
  {:else if tab === 'registrations'}
    <TaxRegistrations />
  {:else if tab === 'exemptions'}
    <TaxExemptions />
  {:else if tab === 'accounts'}
    <TaxAccounts {accountId} />
  {:else if tab === 'packs'}
    <TaxPacks />
  {:else if tab === 'notes'}
    <TaxLegalNotes />
  {:else if tab === 'idchecks'}
    <TaxIdChecks />
  {:else if tab === 'calculator'}
    <TaxCalculator />
  {:else if tab === 'rates'}
    <TaxExchangeRates />
  {/if}
</div>
