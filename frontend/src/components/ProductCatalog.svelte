<script>
  import { onMount } from 'svelte';
  import PageHeader from './PageHeader.svelte';
  import {
    listProductFamilies,
    listCatalogProducts,
    createProductFamily,
    createCatalogProduct,
    updateCatalogProduct,
    listVmTemplates,
    updateFamilyVmConfig,
    updateProductVmConfig,
  } from '../lib/api.js';

  let loading = false;
  let savingVmConfig = false;
  let error = '';
  let families = [];
  let products = [];
  let vmTemplates = [];

  let showFamilyForm = false;
  let showProductForm = false;
  let showEditProductModal = false;
  let fullPageEditor = false;

  let familyForm = { name: '', description: '' };
  let productForm = { family_id: '', name: '', description: '', code: '', vm_template_ids: [] };

  let vmEditor = null;
  let vmEditorForm = null;
  let vmEditorBaseConfig = {};
  let vmSaveSuccess = '';
  let editingProductId = null;
  let editProductForm = { family_id: '', name: '', description: '', code: '', vm_template_ids: [] };

  const vmFields = [
    { key: 'cpu_cores', label: 'CPU Cores', type: 'number' },
    { key: 'ram_mb', label: 'RAM (MB)', type: 'number' },
    { key: 'disk_gb', label: 'Disk (GB)', type: 'number' },
    { key: 'storage', label: 'Storage Target', type: 'text' },
    { key: 'network_bridge', label: 'Default Network Bridge', type: 'text' },
  ];

  function buildVmForm(config = {}, extendsFamily = true) {
    return {
      extends_family: extendsFamily,
      cpu_cores: config.cpu_cores ?? '',
      ram_mb: config.ram_mb ?? '',
      disk_gb: config.disk_gb ?? '',
      storage: config.storage ?? '',
      network_bridge: config.network_bridge ?? '',
      template_ids: Array.isArray(config.template_ids)
        ? config.template_ids.map((v) => String(v))
        : [],
    };
  }

  function vmFormToConfig(form) {
    const out = {};
    for (const field of vmFields) {
      const value = form[field.key];
      if (value === '' || value === null || value === undefined) continue;
      out[field.key] = field.type === 'number' ? Number(value) : value;
    }
    if (Array.isArray(form.template_ids) && form.template_ids.length > 0) {
      out.template_ids = form.template_ids.map((v) => Number(v));
    }
    return out;
  }

  async function loadData() {
    loading = true;
    error = '';
    try {
      const [familyRows, productRows, vmTemplateRows] = await Promise.all([
        listProductFamilies(),
        listCatalogProducts(),
        listVmTemplates(),
      ]);
      families = familyRows;
      products = productRows;
      vmTemplates = vmTemplateRows;
    } catch (err) {
      error = err.message;
    } finally {
      loading = false;
    }
  }

  function productsForFamily(familyId) {
    return products.filter((p) => p.family_id === familyId);
  }

  function getProductById(productId) {
    return products.find((p) => p.id === productId);
  }

  $: ungroupedProducts = products.filter((p) => !p.family_id);

  async function submitFamily() {
    try {
      await createProductFamily({ ...familyForm });
      familyForm = { name: '', description: '' };
      showFamilyForm = false;
      await loadData();
    } catch (err) {
      error = err.message;
    }
  }

  async function submitProduct() {
    try {
      await createCatalogProduct({
        ...productForm,
        family_id: productForm.family_id ? Number(productForm.family_id) : null,
        vm_template_ids: productForm.vm_template_ids.map((v) => Number(v)),
      });
      productForm = { family_id: '', name: '', description: '', code: '', vm_template_ids: [] };
      showProductForm = false;
      await loadData();
    } catch (err) {
      error = err.message;
    }
  }

  function openFamilyVmEditor(family) {
    vmEditor = {
      kind: 'family',
      id: family.id,
      title: `${family.name} (Group)`,
    };
    vmEditorForm = buildVmForm(family.vm_config || {}, true);
    vmEditorBaseConfig = family.vm_config || {};
    fullPageEditor = true;
  }

  function openProductVmEditor(product) {
    const familyConfig = product.family_id
      ? (families.find((f) => f.id === product.family_id)?.vm_config || {})
      : {};
    vmEditor = {
      kind: 'product',
      id: product.id,
      title: `${product.name} (${product.code})`,
      hasFamily: !!product.family_id,
    };
    vmEditorForm = buildVmForm(product.vm_config || {}, product.extends_group_vm_config ?? true);
    if (!product.family_id) vmEditorForm.extends_family = false;
    vmEditorBaseConfig = familyConfig;
    fullPageEditor = true;
  }

  function openEditProduct(product) {
    editingProductId = product.id;
    editProductForm = {
      family_id: product.family_id ? String(product.family_id) : '',
      name: product.name || '',
      description: product.description || '',
      code: product.code || '',
      vm_template_ids: (product.vm_template_ids || []).map((v) => String(v)),
    };
    showEditProductModal = true;
  }

  async function submitEditProduct() {
    if (!editingProductId) return;
    try {
      await updateCatalogProduct(editingProductId, {
        family_id: editProductForm.family_id ? Number(editProductForm.family_id) : null,
        name: editProductForm.name,
        description: editProductForm.description || null,
        code: editProductForm.code,
        vm_template_ids: editProductForm.vm_template_ids.map((v) => Number(v)),
      });
      showEditProductModal = false;
      editingProductId = null;
      await loadData();
    } catch (err) {
      error = err.message;
    }
  }

  function closeVmEditor() {
    fullPageEditor = false;
    vmEditor = null;
    vmEditorForm = null;
    vmEditorBaseConfig = {};
    vmSaveSuccess = '';
  }

  function formatInheritedValue(value) {
    if (value === null || value === undefined || value === '') return 'not set';
    return String(value);
  }

  function getVmTemplateOptionsForEditor() {
    if (!vmEditor) return vmTemplates;
    if (vmEditor.kind === 'family') return vmTemplates;
    const product = getProductById(vmEditor.id);
    if (!product) return vmTemplates;
    const allowed = new Set((product.vm_template_ids || []).map((v) => Number(v)));
    if (!allowed.size) return vmTemplates;
    return vmTemplates.filter((tmpl) => allowed.has(Number(tmpl.id)));
  }

  function formatInheritedTemplates(templateIds = []) {
    if (!Array.isArray(templateIds) || templateIds.length === 0) return 'not set';
    const byId = new Map(vmTemplates.map((tmpl) => [Number(tmpl.id), tmpl]));
    const names = templateIds
      .map((id) => byId.get(Number(id))?.name || `#${id}`)
      .filter(Boolean);
    return names.join(', ');
  }

  async function saveVmConfig() {
    if (!vmEditor || !vmEditorForm) return;
    savingVmConfig = true;
    error = '';
    vmSaveSuccess = '';
    try {
      if (vmEditor.kind === 'family') {
        await updateFamilyVmConfig(vmEditor.id, { config: vmFormToConfig(vmEditorForm) });
      } else {
        await updateProductVmConfig(vmEditor.id, {
          extends_family: !!vmEditorForm.extends_family && !!vmEditor.hasFamily,
          config: vmFormToConfig(vmEditorForm),
        });
      }
      await loadData();
      if (vmEditor?.kind === 'product') {
        const refreshed = getProductById(vmEditor.id);
        if (refreshed) openProductVmEditor(refreshed);
      } else if (vmEditor?.kind === 'family') {
        const refreshed = families.find((f) => f.id === vmEditor.id);
        if (refreshed) openFamilyVmEditor(refreshed);
      }
      const savedTemplates = formatInheritedTemplates(vmFormToConfig(vmEditorForm).template_ids || []);
      vmSaveSuccess = `Saved successfully. Selected templates: ${savedTemplates}.`;
    } catch (err) {
      error = err.message;
    } finally {
      savingVmConfig = false;
    }
  }

  onMount(loadData);
</script>

<PageHeader title="Product Catalog" />
<div class="catalog-page">
  {#if !(fullPageEditor && vmEditor && vmEditorForm)}
    <div class="top-actions">
      <button class="action-btn" on:click={() => { showFamilyForm = true; showProductForm = false; }}>
        New Family / Group
      </button>
      <button class="action-btn" on:click={() => { showProductForm = true; showFamilyForm = false; }}>
        New Product
      </button>
    </div>
  {/if}

  {#if error}
    <div class="error">{error}</div>
  {/if}

  {#if fullPageEditor && vmEditor && vmEditorForm}
    <div class="editor-page">
      <div class="editor-header">
        <h3>VM Config Editor: {vmEditor.title}</h3>
        <button class="secondary-btn" on:click={closeVmEditor}>Back to Catalog</button>
      </div>
      {#if vmEditor.kind === 'product'}
        <label class="toggle">
          <input type="checkbox" bind:checked={vmEditorForm.extends_family} disabled={!vmEditor.hasFamily} />
          Extend group config
        </label>
      {/if}
      <div class="vm-grid">
        {#each vmFields as field}
          <label>
            {field.label}
            {#if field.type === 'number'}
              <input
                type="number"
                bind:value={vmEditorForm[field.key]}
                placeholder={(vmEditor.kind === 'product' && vmEditorForm.extends_family) ? 'override only' : 'set value'}
              />
            {:else}
              <input
                type="text"
                bind:value={vmEditorForm[field.key]}
                placeholder={(vmEditor.kind === 'product' && vmEditorForm.extends_family) ? 'override only' : 'set value'}
              />
            {/if}
            {#if vmEditor.kind === 'product' && vmEditorForm.extends_family && vmEditor.hasFamily}
              <div class="inherited-note">Group: {formatInheritedValue(vmEditorBaseConfig[field.key])}</div>
            {/if}
          </label>
        {/each}
      </div>

      <label class="template-select">
        VM Templates
        <select class="vm-template-list" bind:value={vmEditorForm.template_ids} multiple size="6">
          {#each getVmTemplateOptionsForEditor() as tmpl}
            <option value={String(tmpl.id)}>{tmpl.name} ({tmpl.os_type})</option>
          {/each}
        </select>
        {#if vmEditor.kind === 'product' && vmEditorForm.extends_family && vmEditor.hasFamily}
          <div class="inherited-note">Group: {formatInheritedTemplates(vmEditorBaseConfig.template_ids || [])}</div>
        {/if}
        <div class="selected-note">
          Selected: {formatInheritedTemplates((vmEditorForm.template_ids || []).map((v) => Number(v)))}
        </div>
      </label>
      {#if vmSaveSuccess}
        <div class="save-success">{vmSaveSuccess}</div>
      {/if}
      <button on:click={saveVmConfig} disabled={savingVmConfig}>
        {savingVmConfig ? 'Saving...' : 'Save VM Config'}
      </button>
    </div>
  {:else}
    {#if loading}
      <p>Loading...</p>
    {:else}
      <div class="catalog-table">
        <div class="table-row table-head">
          <div>Name</div>
          <div>Type</div>
          <div>Code</div>
          <div>Count</div>
          <div>Actions</div>
        </div>

        {#each families as family}
          <div class="table-row family-row">
            <div>
              <strong>{family.name}</strong>
              {#if family.description}
                <div class="meta">{family.description}</div>
              {/if}
            </div>
            <div>Group</div>
            <div class="mono">{family.code}</div>
            <div>{productsForFamily(family.id).length}</div>
            <div class="row-actions">
              <button class="tiny-btn" on:click={() => openFamilyVmEditor(family)}>Edit VM Config ↗</button>
            </div>
          </div>

          {#if productsForFamily(family.id).length === 0}
            <div class="table-row child-row empty-row">
              <div>No products in this group</div>
              <div>-</div>
              <div>-</div>
              <div>-</div>
              <div>-</div>
            </div>
          {:else}
            {#each productsForFamily(family.id) as product}
              <div class="table-row child-row">
                <div>
                  <div class="product-name">{product.name}</div>
                  {#if product.description}
                    <div class="meta">{product.description}</div>
                  {/if}
                </div>
                <div>Product</div>
                <div class="mono">{product.code}</div>
                <div>-</div>
                <div class="row-actions">
                  <button class="tiny-btn" on:click={() => openEditProduct(product)}>Edit Product</button>
                  <button class="tiny-btn" on:click={() => openProductVmEditor(product)}>Edit VM Config ↗</button>
                </div>
              </div>
            {/each}
          {/if}
        {/each}

        <div class="table-row section-row">
          <div><strong>Ungrouped Products</strong></div>
          <div>Section</div>
          <div>-</div>
          <div>{ungroupedProducts.length}</div>
          <div>-</div>
        </div>
        {#if ungroupedProducts.length === 0}
          <div class="table-row empty-row">
            <div>No ungrouped products</div>
            <div>-</div>
            <div>-</div>
            <div>-</div>
            <div>-</div>
          </div>
        {:else}
          {#each ungroupedProducts as product}
            <div class="table-row">
              <div>
                <div class="product-name">{product.name}</div>
                {#if product.description}
                  <div class="meta">{product.description}</div>
                {/if}
              </div>
              <div>Product</div>
              <div class="mono">{product.code}</div>
              <div>-</div>
              <div class="row-actions">
                <button class="tiny-btn" on:click={() => openEditProduct(product)}>Edit Product</button>
                <button class="tiny-btn" on:click={() => openProductVmEditor(product)}>Edit VM Config ↗</button>
              </div>
            </div>
          {/each}
        {/if}
      </div>
    {/if}
  {/if}
</div>

{#if showFamilyForm}
  <div class="modal-overlay" role="dialog" aria-modal="true" aria-label="Create family modal">
    <div class="modal-content compact-modal">
      <div class="editor-header">
        <h3>Create Product Family / Group</h3>
        <button class="secondary-btn" on:click={() => (showFamilyForm = false)}>Close</button>
      </div>
      <input bind:value={familyForm.name} placeholder="Family name" />
      <textarea bind:value={familyForm.description} rows="3" placeholder="Description (optional)" />
      <button on:click={submitFamily}>Create Family</button>
    </div>
  </div>
{/if}

{#if showProductForm}
  <div class="modal-overlay" role="dialog" aria-modal="true" aria-label="Create product modal">
    <div class="modal-content compact-modal">
      <div class="editor-header">
        <h3>Create Product</h3>
        <button class="secondary-btn" on:click={() => (showProductForm = false)}>Close</button>
      </div>
      <select bind:value={productForm.family_id}>
        <option value="">No group (ungrouped)</option>
        {#each families as fam}
          <option value={fam.id}>{fam.name} ({fam.code})</option>
        {/each}
      </select>
      <input bind:value={productForm.name} placeholder="Product name" />
      <textarea bind:value={productForm.description} rows="3" placeholder="Description (optional)" />
      <input bind:value={productForm.code} placeholder="product-code" />
      <select bind:value={productForm.vm_template_ids} multiple size="5">
        {#each vmTemplates as tmpl}
          <option value={String(tmpl.id)}>{tmpl.name} ({tmpl.os_type})</option>
        {/each}
      </select>
      <button on:click={submitProduct}>Create Product</button>
    </div>
  </div>
{/if}

{#if showEditProductModal}
  <div class="modal-overlay" role="dialog" aria-modal="true" aria-label="Edit product modal">
    <div class="modal-content compact-modal">
      <div class="editor-header">
        <h3>Edit Product</h3>
        <button class="secondary-btn" on:click={() => { showEditProductModal = false; editingProductId = null; }}>
          Close
        </button>
      </div>
      <select bind:value={editProductForm.family_id}>
        <option value="">No group (ungrouped)</option>
        {#each families as fam}
          <option value={fam.id}>{fam.name} ({fam.code})</option>
        {/each}
      </select>
      <input bind:value={editProductForm.name} placeholder="Product name" />
      <textarea bind:value={editProductForm.description} rows="3" placeholder="Description (optional)" />
      <input bind:value={editProductForm.code} placeholder="product-code" />
      <select bind:value={editProductForm.vm_template_ids} multiple size="5">
        {#each vmTemplates as tmpl}
          <option value={String(tmpl.id)}>{tmpl.name} ({tmpl.os_type})</option>
        {/each}
      </select>
      <button on:click={submitEditProduct}>Save Product</button>
    </div>
  </div>
{/if}

<style>
  .catalog-page { padding: 24px; display: flex; flex-direction: column; gap: 16px; }
  .top-actions { display: flex; gap: 10px; }
  .action-btn { padding: 8px 12px; border: none; border-radius: 6px; background: var(--accent-color); color: white; cursor: pointer; }
  .secondary-btn { padding: 8px 12px; border: 1px solid var(--border-color); border-radius: 6px; background: var(--bg-secondary); color: var(--text-primary); cursor: pointer; }
  .editor-page { background: var(--bg-primary); padding: 16px; border-radius: 10px; border: 1px solid var(--border-color); display: flex; flex-direction: column; gap: 8px; }
  input, select, textarea { background: var(--bg-secondary); border: 1px solid var(--border-color); color: var(--text-primary); border-radius: 6px; padding: 8px; }
  button { width: fit-content; padding: 8px 12px; border: none; border-radius: 6px; background: var(--accent-color); color: white; cursor: pointer; }
  .editor-header { display: flex; justify-content: space-between; align-items: center; gap: 10px; }
  .catalog-table { border: 1px solid var(--border-color); border-radius: 8px; overflow: hidden; }
  .table-row { display: grid; grid-template-columns: minmax(220px, 2fr) 120px minmax(120px, 1fr) 80px 250px; gap: 10px; align-items: center; padding: 7px 10px; border-bottom: 1px solid var(--border-color); font-size: 13px; }
  .table-row:last-child { border-bottom: none; }
  .table-head { background: var(--bg-secondary); font-size: 12px; color: var(--text-secondary); font-weight: 600; text-transform: uppercase; letter-spacing: 0.02em; }
  .family-row { background: color-mix(in srgb, var(--bg-secondary) 45%, transparent); }
  .child-row { padding-left: 24px; }
  .section-row { background: var(--bg-secondary); font-size: 12px; color: var(--text-secondary); }
  .row-actions { display: flex; gap: 6px; align-items: center; }
  .tiny-btn { padding: 3px 7px; font-size: 11px; border: 1px solid var(--border-color); border-radius: 5px; background: var(--bg-secondary); color: var(--text-primary); cursor: pointer; }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; font-size: 12px; }
  .product-name { color: var(--text-primary); }
  .empty-row { color: var(--text-secondary); }
  .vm-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 8px; }
  .vm-grid label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--text-secondary); }
  .template-select { display: flex; flex-direction: column; gap: 6px; font-size: 12px; color: var(--text-secondary); }
  .vm-template-list {
    height: 140px !important;
    min-height: 140px;
    max-height: 220px;
    overflow-y: auto;
    line-height: 1.35;
  }
  .vm-template-list option {
    padding: 4px 6px;
  }
  .inherited-note { font-size: 11px; color: var(--text-secondary); opacity: 0.9; }
  .selected-note { font-size: 11px; color: var(--text-primary); opacity: 0.95; }
  .save-success {
    font-size: 12px;
    color: #7ef0b8;
    background: rgba(18, 110, 74, 0.25);
    border: 1px solid rgba(126, 240, 184, 0.35);
    border-radius: 6px;
    padding: 8px 10px;
  }
  .toggle { display: flex; align-items: center; gap: 6px; color: var(--text-secondary); font-size: 12px; }
  .meta { color: var(--text-secondary); font-size: 12px; }
  .error { color: var(--danger-color); }
  .modal-overlay { position: fixed; inset: 0; background: rgba(0, 0, 0, 0.55); display: flex; align-items: center; justify-content: center; z-index: 1000; }
  .modal-content { width: min(900px, 92vw); max-height: 90vh; overflow: auto; background: var(--bg-primary); border: 1px solid var(--border-color); border-radius: 10px; padding: 16px; display: flex; flex-direction: column; gap: 10px; }
  .compact-modal { width: min(560px, 92vw); }
</style>
