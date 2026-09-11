<script>
  import { onMount } from 'svelte';
  import PageHeader from './PageHeader.svelte';
  import Modal from './ui/Modal.svelte';
  import Button from './ui/Button.svelte';
  import FormGroup from './ui/FormGroup.svelte';
  import Alert from './ui/Alert.svelte';
  import Spinner from './ui/Spinner.svelte';
  import {
    listProductFamilies,
    listCatalogProducts,
    createProductFamily,
    updateProductFamily,
    createCatalogProduct,
    updateCatalogProduct,
    deleteCatalogProduct,
    listPermissionSets,
  } from '../lib/api.js';

  let loading = false;
  let saving = false;
  let error = '';
  let families = [];
  let products = [];
  let permissionSets = [];
  let searchTerm = '';

  let showFamilyForm = false;
  let showProductForm = false;

  let familyForm = { name: '', description: '' };
  let productForm = {
    family_id: '',
    name: '',
    description: '',
    code: '',
    permission_set_id: '',
  };

  let editor = null;
  let identityForm = {};
  let editorSuccess = '';

  async function loadData() {
    loading = true;
    error = '';
    try {
      const [familyRows, productRows, permissionSetRows] = await Promise.all([
        listProductFamilies(),
        listCatalogProducts(),
        listPermissionSets(),
      ]);
      families = (familyRows || []).filter((f) => f.service_type === 'bare_metal');
      const familyIds = new Set(families.map((f) => Number(f.id)));
      products = (productRows || []).filter(
        (p) =>
          p.family_service_type === 'bare_metal' ||
          (p.family_id != null && familyIds.has(Number(p.family_id))),
      );
      permissionSets = permissionSetRows || [];
    } catch (err) {
      error = err.message;
    } finally {
      loading = false;
    }
  }

  function productsForFamily(familyId) {
    return products.filter((p) => Number(p.family_id) === Number(familyId));
  }

  function getProductById(productId) {
    return products.find((p) => p.id === productId);
  }

  function getFamilyById(familyId) {
    return families.find((f) => f.id === familyId);
  }

  $: normalizedSearch = searchTerm.trim().toLowerCase();
  function matchesSearch(entity) {
    if (!normalizedSearch) return true;
    return (
      (entity.name || '').toLowerCase().includes(normalizedSearch) ||
      (entity.code || '').toLowerCase().includes(normalizedSearch) ||
      (entity.description || '').toLowerCase().includes(normalizedSearch)
    );
  }
  $: visibleFamilies = families.filter(matchesSearch);
  $: visibleProducts = products.filter(matchesSearch);

  async function submitFamily() {
    error = '';
    try {
      await createProductFamily({
        name: familyForm.name,
        description: familyForm.description,
        service_type: 'bare_metal',
        provisioning_backend: 'server_group',
      });
      familyForm = { name: '', description: '' };
      showFamilyForm = false;
      await loadData();
    } catch (err) {
      error = err.message;
    }
  }

  async function submitProduct() {
    error = '';
    if (!productForm.family_id) {
      error = 'Select a bare-metal family — products without a family are not typed as bare_metal and will not list here.';
      return;
    }
    try {
      await createCatalogProduct({
        family_id: Number(productForm.family_id),
        name: productForm.name,
        description: productForm.description,
        code: productForm.code,
        permission_set_id: productForm.permission_set_id ? Number(productForm.permission_set_id) : null,
      });
      productForm = {
        family_id: families[0] ? String(families[0].id) : '',
        name: '',
        description: '',
        code: '',
        permission_set_id: '',
      };
      showProductForm = false;
      await loadData();
    } catch (err) {
      error = err.message;
    }
  }

  function openFamilyEditor(family) {
    editor = { kind: 'family', id: family.id, title: family.name, code: family.code };
    identityForm = {
      name: family.name || '',
      description: family.description || '',
    };
    editorSuccess = '';
    error = '';
  }

  function openProductEditor(product) {
    const family = product.family_id ? getFamilyById(product.family_id) : null;
    editor = {
      kind: 'product',
      id: product.id,
      title: product.name,
      code: product.code,
      hasFamily: !!family,
    };
    identityForm = {
      family_id: product.family_id ? String(product.family_id) : '',
      name: product.name || '',
      description: product.description || '',
      code: product.code || '',
      permission_set_id: product.permission_set_id ? String(product.permission_set_id) : '',
    };
    editorSuccess = '';
    error = '';
  }

  function closeEditor() {
    editor = null;
    identityForm = {};
    editorSuccess = '';
  }

  async function saveEditor() {
    if (!editor) return;
    saving = true;
    error = '';
    editorSuccess = '';
    try {
      if (editor.kind === 'family') {
        await updateProductFamily(editor.id, {
          name: identityForm.name,
          description: identityForm.description || null,
        });
      } else {
        if (!identityForm.family_id) {
          throw new Error('Bare-metal products must belong to a bare-metal family.');
        }
        await updateCatalogProduct(editor.id, {
          family_id: Number(identityForm.family_id),
          name: identityForm.name,
          description: identityForm.description || null,
          code: identityForm.code,
          permission_set_id: identityForm.permission_set_id ? Number(identityForm.permission_set_id) : null,
        });
      }
      await loadData();
      if (editor?.kind === 'product') {
        const refreshed = getProductById(editor.id);
        if (refreshed) openProductEditor(refreshed);
      } else if (editor?.kind === 'family') {
        const refreshed = getFamilyById(editor.id);
        if (refreshed) openFamilyEditor(refreshed);
      }
      editorSuccess = 'Saved successfully.';
    } catch (err) {
      error = err.message;
    } finally {
      saving = false;
    }
  }

  async function deleteProduct(product) {
    if (!confirm(`Delete product "${product.name}" (${product.code})? This cannot be undone.`)) return;
    error = '';
    try {
      await deleteCatalogProduct(product.id);
      if (editor?.kind === 'product' && editor.id === product.id) closeEditor();
      await loadData();
    } catch (err) {
      error = err.message;
    }
  }

  onMount(loadData);
</script>

<PageHeader title="Bare Metal Catalog" />
<div class="catalog-page">
  {#if error}
    <Alert type="error">{error}</Alert>
  {/if}

  {#if editor}
    <div class="editor-page">
      <div class="editor-toolbar">
        <button class="back-link" on:click={closeEditor}>&larr; Back to catalog</button>
      </div>
      <div class="editor-heading">
        <span class="kind-badge">{editor.kind === 'family' ? 'Family' : 'Product'}</span>
        <h2>{editor.title}</h2>
        <span class="mono code-chip">{editor.code}</span>
      </div>

      {#if editorSuccess}
        <Alert type="success">{editorSuccess}</Alert>
      {/if}

      <section class="panel">
        <div class="panel-head">
          <h3>Identity</h3>
          <p>Name, description{editor.kind === 'product' ? ', code and family' : ''}.</p>
        </div>
        <div class="field-grid">
          {#if editor.kind === 'product'}
            <FormGroup label="Family" required>
              <select bind:value={identityForm.family_id}>
                <option value="" disabled>Select a family…</option>
                {#each families as fam}
                  <option value={String(fam.id)}>{fam.name} ({fam.code})</option>
                {/each}
              </select>
            </FormGroup>
          {/if}
          <FormGroup label="Name" required>
            <input bind:value={identityForm.name} placeholder="Name" />
          </FormGroup>
          {#if editor.kind === 'product'}
            <FormGroup label="Code" required help="WHMCS Product Code uses this value.">
              <input class="mono" bind:value={identityForm.code} placeholder="product-code" />
            </FormGroup>
          {:else}
            <FormGroup label="Code" help="Family codes are generated on create and not editable.">
              <input class="mono" value={editor.code} disabled />
            </FormGroup>
          {/if}
          <FormGroup label="Description" help="Optional">
            <textarea bind:value={identityForm.description} rows="2" placeholder="Description"></textarea>
          </FormGroup>
          {#if editor.kind === 'product'}
            <FormGroup label="Client permission preset">
              <select bind:value={identityForm.permission_set_id}>
                <option value="">No preset (built-in defaults)</option>
                {#each permissionSets as ps}
                  <option value={String(ps.id)}>{ps.name}</option>
                {/each}
              </select>
            </FormGroup>
          {/if}
        </div>
      </section>

      {#if editor.kind === 'family'}
        <section class="panel">
          <div class="panel-head">
            <h3>Installable OS</h3>
            <p>
              Checkout and first-install OS choices come from the product's
              <a href="/admin/server-groups">server group</a> permitted OS templates,
              not from this catalog page.
            </p>
          </div>
        </section>
      {:else}
        <section class="panel">
          <div class="panel-head">
            <h3>Hardware</h3>
            <p>
              CPU, RAM, and the machine pool are not set here. Put free servers in a
              <a href="/admin/server-groups">server group</a>, then choose that group on the WHMCS product.
            </p>
          </div>
        </section>
      {/if}

      <div class="editor-actions">
        {#if editor.kind === 'product'}
          <Button variant="danger" on:click={() => deleteProduct({ id: editor.id, name: editor.title, code: editor.code })} disabled={saving}>Delete product</Button>
        {/if}
        <div class="editor-actions-spacer"></div>
        <Button variant="secondary" on:click={closeEditor} disabled={saving}>Cancel</Button>
        <Button on:click={saveEditor} disabled={saving}>{saving ? 'Saving…' : 'Save changes'}</Button>
      </div>
    </div>
  {:else}
    <p class="lead">
      Catalog SKUs for WHMCS bare-metal products. Put free servers in a
      <a href="/admin/server-groups">server group</a> and enable its installable OS templates;
      this page only defines the product code and permission preset.
    </p>

    <div class="toolbar">
      <input class="search-input" bind:value={searchTerm} placeholder="Search families, products…" />
      <div class="toolbar-actions">
        <Button variant="secondary" on:click={() => { showFamilyForm = true; }}>New Family</Button>
        <Button on:click={() => {
          productForm = {
            ...productForm,
            family_id: productForm.family_id || (families[0] ? String(families[0].id) : ''),
          };
          showProductForm = true;
        }}>New Product</Button>
      </div>
    </div>

    <div class="fact-grid">
      <section class="fact">
        <h3>Families</h3>
        <p class="fact-value">{families.length}</p>
      </section>
      <section class="fact">
        <h3>Products</h3>
        <p class="fact-value">{products.length}</p>
      </section>
    </div>

    {#if loading}
      <div class="loading-row"><Spinner /> <span>Loading bare-metal catalog…</span></div>
    {:else}
      <section class="panel">
        <div class="panel-head">
          <h3>Installable OS</h3>
          <p>
            Enable OS templates on a <a href="/admin/server-groups">server group</a>, then pick that group
            on the WHMCS product. WHMCS Default OS and checkout OS come from that list.
          </p>
        </div>
      </section>

      <section class="panel">
        <div class="panel-head">
          <h3>Families</h3>
          <p>Bare-metal product families. Installable OS is configured on the server group, not here.</p>
        </div>
        {#if visibleFamilies.length > 0}
          <table class="catalog-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Code</th>
                <th>Description</th>
                <th class="col-count">Products</th>
                <th class="col-actions">Actions</th>
              </tr>
            </thead>
            <tbody>
              {#each visibleFamilies as family (family.id)}
                <tr>
                  <td class="cell-name">{family.name}</td>
                  <td class="mono">{family.code}</td>
                  <td class="cell-desc">{family.description || '—'}</td>
                  <td class="col-count">{productsForFamily(family.id).length}</td>
                  <td class="col-actions">
                    <Button size="small" variant="secondary" on:click={() => openFamilyEditor(family)}>Edit</Button>
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        {:else}
          <p class="muted empty-note">No bare-metal families yet.</p>
        {/if}
      </section>

      <section class="panel">
        <div class="panel-head">
          <h3>Products</h3>
          <p>Sellable catalog entries. The <strong>code</strong> is what WHMCS lists as RackFlow product.</p>
        </div>
        {#if visibleProducts.length > 0}
          <table class="catalog-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Code</th>
                <th>Family</th>
                <th>Permissions</th>
                <th>Description</th>
                <th class="col-actions">Actions</th>
              </tr>
            </thead>
            <tbody>
              {#each visibleProducts as product (product.id)}
                {@const family = product.family_id ? getFamilyById(product.family_id) : null}
                <tr>
                  <td class="cell-name">{product.name}</td>
                  <td class="mono">{product.code}</td>
                  <td>{family ? family.name : '—'}</td>
                  <td class="cell-desc">{product.permission_set_name || '—'}</td>
                  <td class="cell-desc">{product.description || '—'}</td>
                  <td class="col-actions">
                    <Button size="small" variant="secondary" on:click={() => openProductEditor(product)}>Edit</Button>
                    <Button size="small" variant="danger" on:click={() => deleteProduct(product)}>Delete</Button>
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        {:else}
          <p class="muted empty-note">No bare-metal products yet.</p>
        {/if}
      </section>
    {/if}
  {/if}
</div>

{#if showFamilyForm}
  <Modal title="Create Bare Metal Family" onClose={() => (showFamilyForm = false)}>
    <FormGroup label="Family name" required>
      <input bind:value={familyForm.name} placeholder="Family name" />
    </FormGroup>
    <FormGroup label="Description" help="Optional">
      <textarea bind:value={familyForm.description} rows="3" placeholder="Description"></textarea>
    </FormGroup>
    <svelte:fragment slot="footer">
      <Button variant="secondary" on:click={() => (showFamilyForm = false)}>Cancel</Button>
      <Button on:click={submitFamily}>Create Family</Button>
    </svelte:fragment>
  </Modal>
{/if}

{#if showProductForm}
  <Modal title="Create Bare Metal Product" onClose={() => (showProductForm = false)}>
    <FormGroup label="Family" required help="Required — catalog type comes from the family.">
      <select bind:value={productForm.family_id}>
        <option value="" disabled>Select a family…</option>
        {#each families as fam}
          <option value={String(fam.id)}>{fam.name} ({fam.code})</option>
        {/each}
      </select>
    </FormGroup>
    <FormGroup label="Product name" required>
      <input bind:value={productForm.name} placeholder="Product name" />
    </FormGroup>
    <FormGroup label="Description" help="Optional">
      <textarea bind:value={productForm.description} rows="3" placeholder="Description"></textarea>
    </FormGroup>
    <FormGroup label="Code" required help="This is the WHMCS Product Code.">
      <input class="mono" bind:value={productForm.code} placeholder="bm-epyc-32c" />
    </FormGroup>
    <FormGroup label="Client permission preset">
      <select bind:value={productForm.permission_set_id}>
        <option value="">No preset (built-in defaults)</option>
        {#each permissionSets as ps}
          <option value={String(ps.id)}>{ps.name}</option>
        {/each}
      </select>
    </FormGroup>
    <svelte:fragment slot="footer">
      <Button variant="secondary" on:click={() => (showProductForm = false)}>Cancel</Button>
      <Button on:click={submitProduct}>Create Product</Button>
    </svelte:fragment>
  </Modal>
{/if}

<style>
  .catalog-page { padding: 24px; display: flex; flex-direction: column; gap: 16px; }
  @media (max-width: 768px) { .catalog-page { padding: 16px; } }

  input, select, textarea {
    background-color: var(--bg-secondary);
    border: 1px solid var(--border-color);
    color: var(--text-primary);
    border-radius: 6px;
    padding: 8px;
    font-family: inherit;
    font-size: 13px;
  }
  select {
    padding-right: 32px;
    appearance: none;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%23475569' d='M6 8L1 3h10z'/%3E%3C/svg%3E");
    background-repeat: no-repeat;
    background-position: right 10px center;
    background-size: 12px;
    cursor: pointer;
  }
  :global([data-theme="dark"]) select {
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%23cbd5e1' d='M6 8L1 3h10z'/%3E%3C/svg%3E");
  }
  input:disabled { opacity: 0.7; cursor: not-allowed; }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; }

  .lead { margin: 0; font-size: 13px; color: var(--text-secondary); max-width: 72ch; }
  .lead a { color: var(--accent-color); }

  .toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
  .search-input { min-width: 260px; flex: 1 1 260px; max-width: 420px; }
  .toolbar-actions { display: flex; gap: 10px; flex-wrap: wrap; }

  .fact-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
  @media (max-width: 640px) { .fact-grid { grid-template-columns: 1fr; } }
  .fact {
    border: 1px solid var(--border-color);
    border-radius: 12px;
    padding: 14px 16px;
    background: var(--bg-primary);
  }
  .fact h3 {
    margin: 0 0 6px;
    font-size: 12px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-tertiary);
  }
  .fact-value { margin: 0; font-size: 22px; font-weight: 700; color: var(--text-primary); }

  .loading-row { display: flex; align-items: center; gap: 10px; color: var(--text-secondary); padding: 24px 0; }

  .panel {
    border: 1px solid var(--border-color);
    border-radius: 12px;
    padding: 16px 18px;
    background: var(--bg-primary);
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .code-chip {
    font-size: 11px;
    padding: 2px 7px;
    border-radius: 5px;
    background: var(--bg-tertiary);
    color: var(--text-secondary);
    border: 1px solid var(--border-color);
  }
  .empty-note { color: var(--text-secondary); font-size: 13px; padding: 4px; }
  .muted { color: var(--text-secondary); }

  .catalog-table { width: 100%; border-collapse: collapse; font-size: 13px; }
  .catalog-table th, .catalog-table td {
    padding: 10px 12px;
    text-align: left;
    border-bottom: 1px solid var(--border-color);
  }
  .catalog-table thead th {
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-tertiary);
    background: var(--bg-secondary);
    border-bottom: 1px solid var(--border-color);
  }
  .catalog-table tbody tr:hover { background: var(--bg-secondary); }
  .catalog-table tbody tr:last-child td { border-bottom: none; }
  .catalog-table .cell-name { color: var(--text-primary); font-weight: 600; }
  .catalog-table .cell-desc { color: var(--text-secondary); max-width: 36ch; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .catalog-table .col-count { text-align: center; width: 90px; }
  .catalog-table .col-actions { width: 170px; text-align: right; white-space: nowrap; }
  .catalog-table .col-actions :global(.btn) { margin-left: 6px; }
  .catalog-table .col-actions :global(.btn:first-child) { margin-left: 0; }

  .editor-page { display: flex; flex-direction: column; gap: 16px; }
  .editor-toolbar { display: flex; }
  .back-link {
    border: none;
    background: none;
    color: var(--accent-color);
    font-weight: 600;
    font-size: 13px;
    cursor: pointer;
    padding: 4px 0;
  }
  .back-link:hover { text-decoration: underline; }
  .editor-heading { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .editor-heading h2 { margin: 0; font-size: 1.3rem; }
  .kind-badge {
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    padding: 3px 8px;
    border-radius: 6px;
    background: color-mix(in srgb, var(--accent-color) 16%, transparent);
    color: var(--accent-color);
  }
  .panel-head h3 { margin: 0 0 4px; font-size: 1rem; }
  .panel-head p { margin: 0; font-size: 13px; color: var(--text-secondary); max-width: 60ch; }
  .panel-head a { color: var(--accent-color); }

  .field-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px 16px; }

  .editor-actions { display: flex; align-items: center; gap: 10px; }
  .editor-actions-spacer { flex: 1; }
</style>
