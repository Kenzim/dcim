<script>
  import { onMount } from 'svelte';
  import PageHeader from './PageHeader.svelte';
  import MultiSelect from './ui/MultiSelect.svelte';
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
    listVmTemplates,
    updateFamilyVmConfig,
    updateProductVmConfig,
    listPermissionSets,
    listProxmoxBackupStorages,
  } from '../lib/api.js';

  let loading = false;
  let saving = false;
  let error = '';
  let families = [];
  let products = [];
  let vmTemplates = [];
  let permissionSets = [];
  let backupStorages = [];
  let searchTerm = '';

  let showFamilyForm = false;
  let showProductForm = false;

  let familyForm = { name: '', description: '' };
  let productForm = {
    family_id: '',
    name: '',
    description: '',
    code: '',
    vm_template_ids: [],
  };

  // Unified editor: one full-page editor covering Identity + VM specs + Templates,
  // replacing the old separate "Edit Product" / "Edit VM Config" surfaces.
  let editor = null; // { kind: 'family' | 'product', id, title, code, hasFamily }
  let identityForm = {};
  let specsForm = {};
  let inheritedConfig = {};
  let editorSuccess = '';

  const specFields = [
    { key: 'cpu_cores', label: 'CPU Cores', type: 'number' },
    { key: 'ram_mb', label: 'RAM (MB)', type: 'number' },
    { key: 'disk_gb', label: 'Disk (GB)', type: 'number' },
    { key: 'storage', label: 'Storage Target', type: 'text' },
    { key: 'network_bridge', label: 'Default Network Bridge', type: 'text' },
  ];

  function cloneModeFromConfig(config = {}) {
    if (config.full_clone === true) return 'full';
    if (config.full_clone === false) return 'linked';
    return '';
  }

  function buildSpecsForm(config = {}, extendsFamily = true) {
    return {
      extends_family: extendsFamily,
      cpu_cores: config.cpu_cores ?? '',
      ram_mb: config.ram_mb ?? '',
      disk_gb: config.disk_gb ?? '',
      storage: config.storage ?? '',
      network_bridge: config.network_bridge ?? '',
      clone_mode: cloneModeFromConfig(config),
      platform_backup_storage: config.platform_backup_storage ?? '',
      client_backup_storage: config.client_backup_storage ?? '',
      max_client_backups:
        config.max_client_backups === undefined || config.max_client_backups === null
          ? ''
          : String(config.max_client_backups),
      template_ids: Array.isArray(config.template_ids)
        ? config.template_ids.map((v) => String(v))
        : [],
    };
  }

  function specsFormToConfig(form) {
    const out = {};
    for (const field of specFields) {
      const value = form[field.key];
      if (value === '' || value === null || value === undefined) continue;
      out[field.key] = field.type === 'number' ? Number(value) : value;
    }
    if (form.clone_mode === 'full') {
      out.full_clone = true;
    } else if (form.clone_mode === 'linked') {
      out.full_clone = false;
    }
    if (form.platform_backup_storage) {
      out.platform_backup_storage = String(form.platform_backup_storage);
    }
    if (form.client_backup_storage) {
      out.client_backup_storage = String(form.client_backup_storage);
    }
    if (form.max_client_backups !== '' && form.max_client_backups !== null && form.max_client_backups !== undefined) {
      out.max_client_backups = Number(form.max_client_backups);
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
      const [familyRows, productRows, vmTemplateRows, permissionSetRows, backupStorageRows] = await Promise.all([
        listProductFamilies(),
        listCatalogProducts(),
        listVmTemplates(),
        listPermissionSets(),
        listProxmoxBackupStorages().catch(() => []),
      ]);
      // VM Product Catalog: Proxmox families/products only (proxy lives under Proxy Catalog).
      // Ungrouped products are listed under Proxy Catalog (legacy "No family" creates).
      families = (familyRows || []).filter((f) => f.service_type === 'vm');
      const vmFamilyIds = new Set(families.map((f) => Number(f.id)));
      products = (productRows || []).filter(
        (p) => p.family_service_type === 'vm' || (p.family_id != null && vmFamilyIds.has(Number(p.family_id))),
      );
      vmTemplates = vmTemplateRows;
      permissionSets = permissionSetRows;
      backupStorages = backupStorageRows || [];
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

  function getFamilyById(familyId) {
    return families.find((f) => f.id === familyId);
  }

  $: vmTemplateOptions = vmTemplates.map((tmpl) => ({
    value: String(tmpl.id),
    label: `${tmpl.name} (${tmpl.os_type})`,
  }));

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
    try {
      await createProductFamily({
        name: familyForm.name,
        description: familyForm.description,
        service_type: 'vm',
      });
      familyForm = { name: '', description: '' };
      showFamilyForm = false;
      await loadData();
    } catch (err) {
      error = err.message;
    }
  }

  async function submitProduct() {
    try {
      const payload = {
        family_id: productForm.family_id ? Number(productForm.family_id) : null,
        name: productForm.name,
        description: productForm.description,
        code: productForm.code,
        vm_template_ids: productForm.vm_template_ids.map((v) => Number(v)),
      };
      await createCatalogProduct(payload);
      productForm = {
        family_id: '',
        name: '',
        description: '',
        code: '',
        vm_template_ids: [],
      };
      showProductForm = false;
      await loadData();
    } catch (err) {
      error = err.message;
    }
  }

  function openFamilyEditor(family) {
    editor = { kind: 'family', id: family.id, title: family.name, code: family.code };
    identityForm = { name: family.name || '', description: family.description || '' };
    specsForm = buildSpecsForm(family.vm_config || {}, true);
    inheritedConfig = {};
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
      vm_template_ids: (product.vm_template_ids || []).map((v) => String(v)),
      permission_set_id: product.permission_set_id ? String(product.permission_set_id) : '',
    };
    specsForm = buildSpecsForm(product.vm_config || {}, product.extends_group_vm_config ?? true);
    if (!family) specsForm.extends_family = false;
    inheritedConfig = family?.vm_config || {};
    editorSuccess = '';
    error = '';
  }

  function closeEditor() {
    editor = null;
    identityForm = {};
    specsForm = {};
    inheritedConfig = {};
    editorSuccess = '';
  }

  $: if (editor?.kind === 'product') {
    const fam = identityForm.family_id ? getFamilyById(Number(identityForm.family_id)) : null;
    if (!!fam !== !!editor.hasFamily) {
      editor = { ...editor, hasFamily: !!fam };
      inheritedConfig = fam?.vm_config || {};
    }
  }

  // Templates a product/family can actually select as defaults: filtered live by
  // the allow-list being edited in the Identity/Templates section (product only).
  $: editorTemplateOptions = (() => {
    if (!editor) return [];
    if (editor.kind === 'family') return vmTemplateOptions;
    const allowed = new Set((identityForm.vm_template_ids || []).map((v) => Number(v)));
    if (!allowed.size) return vmTemplateOptions;
    return vmTemplateOptions.filter((opt) => allowed.has(Number(opt.value)));
  })();

  function formatInheritedValue(value) {
    if (value === null || value === undefined || value === '') return 'not set';
    return String(value);
  }

  function formatInheritedTemplates(templateIds = []) {
    if (!Array.isArray(templateIds) || templateIds.length === 0) return 'not set';
    const byId = new Map(vmTemplates.map((tmpl) => [Number(tmpl.id), tmpl]));
    const names = templateIds
      .map((id) => byId.get(Number(id))?.name || `#${id}`)
      .filter(Boolean);
    return names.join(', ');
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
        await updateFamilyVmConfig(editor.id, { config: specsFormToConfig(specsForm) });
      } else {
        await updateCatalogProduct(editor.id, {
          family_id: identityForm.family_id ? Number(identityForm.family_id) : null,
          name: identityForm.name,
          description: identityForm.description || null,
          code: identityForm.code,
          vm_template_ids: identityForm.vm_template_ids.map((v) => Number(v)),
          permission_set_id: identityForm.permission_set_id ? Number(identityForm.permission_set_id) : null,
        });
        await updateProductVmConfig(editor.id, {
          extends_family: !!specsForm.extends_family && !!editor.hasFamily,
          config: specsFormToConfig(specsForm),
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

<PageHeader title="VM Product Catalog" />
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
        <span class="kind-badge">{editor.kind === 'family' ? 'Group' : 'Product'}</span>
        <h2>{editor.title}</h2>
        <span class="mono code-chip">{editor.code}</span>
      </div>

      {#if editorSuccess}
        <Alert type="success">{editorSuccess}</Alert>
      {/if}

      <section class="panel">
        <div class="panel-head">
          <h3>Identity</h3>
          <p>Name, description{editor.kind === 'product' ? ', code and group' : ''}.</p>
        </div>
        <div class="field-grid">
          {#if editor.kind === 'product'}
            <FormGroup label="Group">
              <select bind:value={identityForm.family_id}>
                <option value="">No group (ungrouped)</option>
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
            <FormGroup label="Code" required>
              <input class="mono" bind:value={identityForm.code} placeholder="product-code" />
            </FormGroup>
          {:else}
            <FormGroup label="Code" help="Group codes are generated on create and not editable.">
              <input class="mono" value={editor.code} disabled />
            </FormGroup>
          {/if}
          <FormGroup label="Description" help="Optional">
            <textarea bind:value={identityForm.description} rows="2" placeholder="Description"></textarea>
          </FormGroup>
          {#if editor.kind === 'product'}
            <FormGroup label="Client permission preset" help="Applied to services created from this product; a per-client or per-service preset can still override it.">
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

      <section class="panel">
        <div class="panel-head">
          <h3>VM specs</h3>
          <p>
            {#if editor.kind === 'product' && editor.hasFamily}
              Overrides on top of the group defaults below. Leave a field blank to inherit it.
            {:else}
              Default sizing applied when provisioning from this {editor.kind === 'family' ? 'group' : 'product'}.
            {/if}
          </p>
        </div>
        {#if editor.kind === 'product' && editor.hasFamily}
          <label class="toggle">
            <input type="checkbox" bind:checked={specsForm.extends_family} />
            Extend group config
          </label>
        {/if}
        <div class="field-grid spec-grid">
          {#each specFields as field}
            <FormGroup label={field.label}>
              {#if field.type === 'number'}
                <input
                  type="number"
                  bind:value={specsForm[field.key]}
                  placeholder={(editor.kind === 'product' && specsForm.extends_family && editor.hasFamily) ? 'override only' : 'set value'}
                />
              {:else}
                <input
                  type="text"
                  bind:value={specsForm[field.key]}
                  placeholder={(editor.kind === 'product' && specsForm.extends_family && editor.hasFamily) ? 'override only' : 'set value'}
                />
              {/if}
              {#if editor.kind === 'product' && specsForm.extends_family && editor.hasFamily}
                <div class="inherited-note">Group: {formatInheritedValue(inheritedConfig[field.key])}</div>
              {/if}
            </FormGroup>
          {/each}
          <FormGroup label="Clone mode">
            <select bind:value={specsForm.clone_mode}>
              {#if editor.kind === 'product' && specsForm.extends_family && editor.hasFamily}
                <option value="">Inherit from group (linked default)</option>
              {:else}
                <option value="">Linked clone (default)</option>
              {/if}
              <option value="linked">Linked clone</option>
              <option value="full">Full clone</option>
            </select>
            <div class="inherited-note">
              Linked clones are faster and share the template disk. Use full clone when the VM must be independent of the template.
            </div>
            {#if editor.kind === 'product' && specsForm.extends_family && editor.hasFamily}
              <div class="inherited-note">
                Group: {inheritedConfig.full_clone === true ? 'Full clone' : inheritedConfig.full_clone === false ? 'Linked clone' : 'Linked clone (default)'}
              </div>
            {/if}
          </FormGroup>
        </div>
      </section>

      <section class="panel">
        <div class="panel-head">
          <h3>VM backups</h3>
          <p>
            Select two Proxmox storage IDs (typically PBS with namespaces baked into each storage).
            Platform backups are scheduled in Proxmox and shown read-only; client backups are created/deleted via RackFlow.
          </p>
        </div>
        <div class="field-grid spec-grid">
          <FormGroup label="Platform backup storage">
            <select bind:value={specsForm.platform_backup_storage}>
              <option value="">
                {(editor.kind === 'product' && specsForm.extends_family && editor.hasFamily)
                  ? 'Inherit from group'
                  : 'Not set'}
              </option>
              {#each backupStorages as st}
                <option value={st.storage_name}>{st.storage_name}{st.storage_type ? ` (${st.storage_type})` : ''}</option>
              {/each}
            </select>
            {#if editor.kind === 'product' && specsForm.extends_family && editor.hasFamily}
              <div class="inherited-note">Group: {formatInheritedValue(inheritedConfig.platform_backup_storage)}</div>
            {/if}
          </FormGroup>
          <FormGroup label="Client backup storage">
            <select bind:value={specsForm.client_backup_storage}>
              <option value="">
                {(editor.kind === 'product' && specsForm.extends_family && editor.hasFamily)
                  ? 'Inherit from group'
                  : 'Not set'}
              </option>
              {#each backupStorages as st}
                <option value={st.storage_name}>{st.storage_name}{st.storage_type ? ` (${st.storage_type})` : ''}</option>
              {/each}
            </select>
            {#if editor.kind === 'product' && specsForm.extends_family && editor.hasFamily}
              <div class="inherited-note">Group: {formatInheritedValue(inheritedConfig.client_backup_storage)}</div>
            {/if}
          </FormGroup>
          <FormGroup label="Max client backups">
            <input
              type="number"
              min="0"
              bind:value={specsForm.max_client_backups}
              placeholder={(editor.kind === 'product' && specsForm.extends_family && editor.hasFamily) ? 'override only' : 'e.g. 3'}
            />
            {#if editor.kind === 'product' && specsForm.extends_family && editor.hasFamily}
              <div class="inherited-note">Group: {formatInheritedValue(inheritedConfig.max_client_backups)}</div>
            {/if}
          </FormGroup>
        </div>
      </section>

      <section class="panel">
        <div class="panel-head">
          <h3>VM templates</h3>
          <p>
            {#if editor.kind === 'product'}
              Choose which catalog templates this product may use, then pick the default(s) offered at provision time.
            {:else}
              Default template selection inherited by products in this group.
            {/if}
          </p>
        </div>
        {#if editor.kind === 'product'}
          <MultiSelect
            label="Allowed templates (access list)"
            options={vmTemplateOptions}
            bind:value={identityForm.vm_template_ids}
            size={5}
            emptyText="No VM templates available"
          />
        {/if}
        <MultiSelect
          label={editor.kind === 'product' ? 'Default templates' : 'Group default templates'}
          options={editorTemplateOptions}
          bind:value={specsForm.template_ids}
          size={5}
          emptyText="No VM templates available"
        />
        {#if editor.kind === 'product' && specsForm.extends_family && editor.hasFamily}
          <div class="inherited-note">Group default: {formatInheritedTemplates(inheritedConfig.template_ids || [])}</div>
        {/if}
        <div class="selected-note">
          Selected: {formatInheritedTemplates((specsForm.template_ids || []).map((v) => Number(v)))}
        </div>
      </section>

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
    <div class="toolbar">
      <input class="search-input" bind:value={searchTerm} placeholder="Search groups and products…" />
      <div class="toolbar-actions">
        <Button variant="secondary" on:click={() => { showFamilyForm = true; }}>New Family / Group</Button>
        <Button on:click={() => { showProductForm = true; }}>New Product</Button>
      </div>
    </div>

    <div class="fact-grid">
      <section class="fact">
        <h3>Groups</h3>
        <p class="fact-value">{families.length}</p>
      </section>
      <section class="fact">
        <h3>Products</h3>
        <p class="fact-value">{products.length}</p>
      </section>
      <section class="fact">
        <h3>VM Templates</h3>
        <p class="fact-value">{vmTemplates.length}</p>
      </section>
    </div>

    {#if loading}
      <div class="loading-row"><Spinner /> <span>Loading catalog…</span></div>
    {:else}
      <section class="panel">
        <div class="panel-head">
          <h3>Groups</h3>
          <p>Families that products belong to and inherit default VM specs/templates from.</p>
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
          <p class="muted empty-note">No groups match "{searchTerm}".</p>
        {/if}
      </section>

      <section class="panel">
        <div class="panel-head">
          <h3>Products</h3>
          <p>Sellable catalog entries, each optionally belonging to a group.</p>
        </div>
        {#if visibleProducts.length > 0}
          <table class="catalog-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Code</th>
                <th>Group</th>
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
          <p class="muted empty-note">No products match "{searchTerm}".</p>
        {/if}
      </section>
    {/if}
  {/if}
</div>

{#if showFamilyForm}
  <Modal title="Create VM Product Family / Group" onClose={() => (showFamilyForm = false)}>
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
  <Modal title="Create VM Product" onClose={() => (showProductForm = false)}>
    <FormGroup label="Group">
      <select bind:value={productForm.family_id}>
        <option value="">No group (ungrouped)</option>
        {#each families as fam}
          <option value={fam.id}>{fam.name} ({fam.code})</option>
        {/each}
      </select>
    </FormGroup>
    <FormGroup label="Product name" required>
      <input bind:value={productForm.name} placeholder="Product name" />
    </FormGroup>
    <FormGroup label="Description" help="Optional">
      <textarea bind:value={productForm.description} rows="3" placeholder="Description"></textarea>
    </FormGroup>
    <FormGroup label="Code" required>
      <input bind:value={productForm.code} placeholder="product-code" />
    </FormGroup>
    <MultiSelect
      label="VM Templates"
      options={vmTemplateOptions}
      bind:value={productForm.vm_template_ids}
      size={5}
      emptyText="No VM templates available"
    />
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

  /* Toolbar */
  .toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
  .search-input { min-width: 260px; flex: 1 1 260px; max-width: 420px; }
  .toolbar-actions { display: flex; gap: 10px; }

  /* Fact strip */
  .fact-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
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
  .meta { margin: 2px 0 0; color: var(--text-secondary); font-size: 12px; }
  .empty-note { color: var(--text-secondary); font-size: 13px; padding: 4px; }
  .muted { color: var(--text-secondary); }

  /* Catalog tables */
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

  /* Editor page */
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

  .field-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px 16px; }
  .spec-grid { grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); }
  .inherited-note { font-size: 11px; color: var(--text-secondary); opacity: 0.9; margin-top: 4px; }
  .selected-note { font-size: 12px; color: var(--text-primary); opacity: 0.95; }
  .toggle { display: flex; align-items: center; gap: 6px; color: var(--text-secondary); font-size: 13px; font-weight: 600; }

  .editor-actions { display: flex; align-items: center; gap: 10px; }
  .editor-actions-spacer { flex: 1; }
</style>
