<?php
/**
 * RackFlow reseller product-save hook.
 *
 * The hook is deliberately limited to products whose servertype is exactly
 * rackflow_reseller. It synchronizes only zero-priced OS choices and never
 * changes the WHMCS product's retail pricing.
 */

if (!defined('WHMCS')) {
    die('This file cannot be accessed directly');
}

require_once __DIR__ . '/rackflow_reseller.php';

function rackflow_reseller_hookEnabled($value)
{
    return in_array(strtolower(trim((string)$value)), array('1', 'on', 'yes', 'true'), true);
}

function rackflow_reseller_ensureSshKeysCustomField($productId)
{
    if (empty($productId) || !class_exists('\Illuminate\Database\Capsule\Manager')) {
        return null;
    }
    $capsule = '\Illuminate\Database\Capsule\Manager';
    $field = $capsule::table('tblcustomfields')
        ->where('type', 'product')
        ->where('relid', (int)$productId)
        ->where('fieldname', RACKFLOW_RESELLER_SSH_KEYS_FIELD_NAME)
        ->first();
    if (!$field) {
        return (int)$capsule::table('tblcustomfields')->insertGetId(array(
            'type' => 'product',
            'relid' => (int)$productId,
            'fieldname' => RACKFLOW_RESELLER_SSH_KEYS_FIELD_NAME,
            'fieldtype' => 'textarea',
            'description' => 'Optional OpenSSH public keys, one per line.',
            'required' => '',
            'showorder' => 'on',
            'showinvoice' => '',
            'adminonly' => '',
        ));
    }
    $capsule::table('tblcustomfields')->where('id', (int)$field->id)->update(array(
        'fieldtype' => 'textarea',
        'showorder' => 'on',
        'adminonly' => '',
    ));
    return (int)$field->id;
}

function rackflow_reseller_ensureOsChoicePricing($choiceId)
{
    if (empty($choiceId) || !class_exists('\Illuminate\Database\Capsule\Manager')) {
        return;
    }
    $capsule = '\Illuminate\Database\Capsule\Manager';
    foreach ($capsule::table('tblcurrencies')->pluck('id') as $currencyId) {
        $exists = $capsule::table('tblpricing')
            ->where('type', 'configoptions')
            ->where('currency', (int)$currencyId)
            ->where('relid', (int)$choiceId)
            ->exists();
        if (!$exists) {
            $capsule::table('tblpricing')->insert(array(
                'type' => 'configoptions',
                'currency' => (int)$currencyId,
                'relid' => (int)$choiceId,
                'msetupfee' => '0.00',
                'qsetupfee' => '0.00',
                'ssetupfee' => '0.00',
                'asetupfee' => '0.00',
                'bsetupfee' => '0.00',
                'tsetupfee' => '0.00',
                'monthly' => '0.00',
                'quarterly' => '0.00',
                'semiannually' => '0.00',
                'annually' => '0.00',
                'biennially' => '0.00',
                'triennially' => '0.00',
            ));
        }
    }
}

function rackflow_reseller_syncOsOption($productId, array $catalogProduct, $enabled)
{
    if (empty($productId) || !class_exists('\Illuminate\Database\Capsule\Manager')) {
        return false;
    }
    $capsule = '\Illuminate\Database\Capsule\Manager';
    $groupName = 'RackFlow Reseller Operating System';
    $group = $capsule::table('tblproductconfiggroups')->where('name', $groupName)->first();
    $groupId = $group
        ? (int)$group->id
        : (int)$capsule::table('tblproductconfiggroups')->insertGetId(array(
            'name' => $groupName,
            'description' => 'OS choices synchronized from an allowed RackFlow reseller product.',
        ));
    if (!$capsule::table('tblproductconfiglinks')->where('gid', $groupId)->where('pid', (int)$productId)->exists()) {
        $capsule::table('tblproductconfiglinks')->insert(array('gid' => $groupId, 'pid' => (int)$productId));
    }
    $option = $capsule::table('tblproductconfigoptions')
        ->where('gid', $groupId)
        ->where('optionname', 'Operating System')
        ->first();
    $optionId = $option
        ? (int)$option->id
        : (int)$capsule::table('tblproductconfigoptions')->insertGetId(array(
            'gid' => $groupId,
            'optionname' => 'Operating System',
            'optiontype' => 1,
            'qtyminimum' => 0,
            'qtymaximum' => 0,
            'order' => 0,
            'hidden' => 1,
        ));

    $choices = array();
    if ($enabled) {
        $mode = isset($catalogProduct['checkout_os_mode']) ? $catalogProduct['checkout_os_mode'] : 'none';
        if ($mode === 'vm_template') {
            foreach (isset($catalogProduct['vm_templates']) && is_array($catalogProduct['vm_templates']) ? $catalogProduct['vm_templates'] : array() as $template) {
                if (!empty($template['id'])) {
                    $label = !empty($template['name']) ? $template['name'] : ('Template ' . $template['id']);
                    $choices[] = 'rfvt:' . (int)$template['id'] . '|' . $label;
                }
            }
        } else {
            foreach (isset($catalogProduct['os_profiles']) && is_array($catalogProduct['os_profiles']) ? $catalogProduct['os_profiles'] : array() as $profile) {
                if (!empty($profile['code'])) {
                    $label = !empty($profile['name']) ? $profile['name'] : $profile['code'];
                    $choices[] = 'rfos:' . $profile['code'] . '|' . $label;
                }
            }
        }
    }
    $capsule::table('tblproductconfigoptions')
        ->where('id', $optionId)
        ->update(array('hidden' => empty($choices) ? 1 : 0, 'optiontype' => 1));

    $keep = array();
    $position = 0;
    foreach ($choices as $choiceName) {
        $position++;
        $keep[$choiceName] = true;
        $choice = $capsule::table('tblproductconfigoptionssub')
            ->where('configid', $optionId)
            ->where('optionname', $choiceName)
            ->first();
        if ($choice) {
            $choiceId = (int)$choice->id;
            $capsule::table('tblproductconfigoptionssub')->where('id', $choiceId)->update(array(
                'sortorder' => $position,
                'hidden' => 0,
            ));
        } else {
            $choiceId = (int)$capsule::table('tblproductconfigoptionssub')->insertGetId(array(
                'configid' => $optionId,
                'optionname' => $choiceName,
                'sortorder' => $position,
                'hidden' => 0,
            ));
        }
        rackflow_reseller_ensureOsChoicePricing($choiceId);
    }
    foreach ($capsule::table('tblproductconfigoptionssub')->where('configid', $optionId)->get() as $existing) {
        if (!isset($keep[(string)$existing->optionname])) {
            $capsule::table('tblproductconfigoptionssub')->where('id', (int)$existing->id)->update(array('hidden' => 1));
        }
    }
    return true;
}

/**
 * Product-details UI cleanup for RackFlow Reseller (Twenty-One + Lagom 2):
 * hide Configurable Options / Additional Information / Change Password tabs,
 * hide redundant Server Information chrome, and lift the custom card out of
 * nested theme panels so Lagom status badges no longer clip over it.
 */
add_hook('ClientAreaFooterOutput', 1, function (array $vars) {
    $filename = isset($vars['filename']) ? (string)$vars['filename'] : '';
    $action = isset($_GET['action']) ? (string)$_GET['action'] : '';
    $templateFile = isset($vars['templatefile']) ? (string)$vars['templatefile'] : '';
    $onProductDetails = (
        $templateFile === 'clientareaproductdetails'
        || ($filename === 'clientarea' && $action === 'productdetails')
        || (isset($_GET['id']) && $action === 'productdetails')
    );
    if (!$onProductDetails) {
        return '';
    }

    $module = '';
    if (!empty($vars['module'])) {
        $module = strtolower((string)$vars['module']);
    } elseif (!empty($vars['servertype'])) {
        $module = strtolower((string)$vars['servertype']);
    }
    if ($module !== '' && $module !== 'rackflow_reseller') {
        return '';
    }

    return <<<'HTML'
<style id="rackflow-reseller-client-area-hide-tabs">
  body:has(#rackflow-reseller-client-area) .nav-tabs a[href="#configoptions"],
  body:has(#rackflow-reseller-client-area) li.nav-item:has(> a[href="#configoptions"]),
  body:has(#rackflow-reseller-client-area) li:has(> a[href="#configoptions"]),
  body:has(#rackflow-reseller-client-area) #configoptions,
  body:has(#rackflow-reseller-client-area) .nav-tabs a[href="#additionalinfo"],
  body:has(#rackflow-reseller-client-area) li.nav-item:has(> a[href="#additionalinfo"]),
  body:has(#rackflow-reseller-client-area) li:has(> a[href="#additionalinfo"]),
  body:has(#rackflow-reseller-client-area) #additionalinfo,
  body:has(#rackflow-reseller-client-area) a[href="#tabChangepw"],
  body:has(#rackflow-reseller-client-area) li:has(> a[href="#tabChangepw"]),
  body:has(#rackflow-reseller-client-area) #tabChangepw,
  body:has(#rackflow-reseller-client-area) a[href="#Changepw"],
  body:has(#rackflow-reseller-client-area) li:has(> a[href="#Changepw"]),
  body:has(#rackflow-reseller-client-area) #Changepw,
  body:has(#rackflow-reseller-client-area) a[href="#domain"],
  body:has(#rackflow-reseller-client-area) li:has(> a[href="#domain"]),
  body:has(#rackflow-reseller-client-area) .list-group-item[href="#domain"] {
    display: none !important;
  }
  body:has(#rackflow-reseller-client-area) #domain > .row,
  body:has(#rackflow-reseller-client-area) #manage > .row {
    display: none !important;
  }
  body:has(#rackflow-reseller-client-area) #domain .module-client-area,
  body:has(#rackflow-reseller-client-area) #manage .module-client-area {
    display: block !important;
    text-align: left;
  }
  #rackflow-reseller-client-area-slot {
    margin: 0 0 18px;
    width: 100%;
  }
  #rackflow-reseller-client-area-slot .rfr-ca,
  #rackflow-reseller-client-area-slot .rfr-ca__card {
    width: 100%;
    max-width: none;
  }
  body.lagom #rackflow-reseller-client-area-slot,
  body.lagom .module-client-area.module-rackflow_reseller {
    width: 100%;
  }
  body.lagom.rfr-ca-unwrapped .panel-product-details:not(:has(#rackflow-reseller-client-area)):not(:has(.module-client-area)) {
    display: none !important;
  }
  body.rfr-ca-unwrapped .rfr-ca-hide-tabs,
  body.rfr-ca-unwrapped .rfr-ca-hide-tabs-connector,
  body.rfr-ca-unwrapped .rfr-ca-hide-tabpane,
  body.rfr-ca-unwrapped .rfr-ca-hide-section {
    display: none !important;
  }
  body.rfr-ca-unwrapped .rfr-ca-plain-tabs.product-details-tab-container {
    background: transparent !important;
    border: 0 !important;
    box-shadow: none !important;
    padding: 0 !important;
  }
</style>
<script>
(function () {
  function hideEl(el) {
    if (el) { el.style.display = 'none'; }
  }

  function hideHrefTargets(hrefs) {
    for (var h = 0; h < hrefs.length; h++) {
      var links = document.querySelectorAll('a[href="' + hrefs[h] + '"]');
      for (var i = 0; i < links.length; i++) {
        hideEl(links[i].closest('li') || links[i]);
      }
      var paneId = hrefs[h].replace(/^#/, '');
      hideEl(document.getElementById(paneId));
    }
  }

  function ensureSlotBefore(anchor) {
    var slot = document.getElementById('rackflow-reseller-client-area-slot');
    if (!slot) {
      slot = document.createElement('div');
      slot.id = 'rackflow-reseller-client-area-slot';
    }
    if (anchor && anchor.parentNode) {
      if (slot.parentNode !== anchor.parentNode || slot.nextSibling !== anchor) {
        anchor.parentNode.insertBefore(slot, anchor);
      }
    }
    return slot;
  }

  function unwrapLagom(card) {
    var lagomPanel = card.closest('.panel-product-details');
    var productDetails = document.querySelector('#Overview .product-details');
    var anchor = lagomPanel || card.closest('.module-client-area');
    if (!anchor) {
      return false;
    }

    var slot;
    if (productDetails && productDetails.parentNode) {
      slot = document.getElementById('rackflow-reseller-client-area-slot');
      if (!slot) {
        slot = document.createElement('div');
        slot.id = 'rackflow-reseller-client-area-slot';
      }
      if (productDetails.nextSibling !== slot) {
        productDetails.parentNode.insertBefore(slot, productDetails.nextSibling);
      }
    } else {
      slot = ensureSlotBefore(anchor);
    }
    slot.appendChild(card);

    if (lagomPanel && !lagomPanel.contains(card)) {
      lagomPanel.classList.add('rfr-ca-hide-section');
      hideEl(lagomPanel);
    }

    var domainPane = document.getElementById('domain');
    if (domainPane) {
      var detailsSection = domainPane.closest('.section');
      if (detailsSection) {
        detailsSection.classList.add('rfr-ca-hide-section');
        hideEl(detailsSection);
      } else {
        hideEl(domainPane);
      }
    }

    hideHrefTargets(['#Changepw', '#tabChangepw', '#configoptions', '#additionalinfo', '#domain']);
    hideEl(document.getElementById('Changepw'));

    var overview = document.getElementById('Overview');
    if (overview) {
      overview.classList.add('active');
      overview.style.display = '';
      overview.classList.remove('rfr-ca-hide-tabpane');
    }

    card.dataset.rfrUnwrapped = '1';
    document.body.classList.add('rfr-ca-unwrapped');
    return true;
  }

  function unwrapLegacy(card) {
    hideHrefTargets(['#configoptions', '#additionalinfo', '#tabChangepw', '#Changepw']);

    var domainRows = document.querySelectorAll('#domain > .row, #manage > .row');
    for (var r = 0; r < domainRows.length; r++) {
      hideEl(domainRows[r]);
    }

    var tabPane = card.closest('#domain, #manage');
    if (!tabPane) {
      card.dataset.rfrUnwrapped = '1';
      document.body.classList.add('rfr-ca-unwrapped');
      return;
    }

    var tabContent = tabPane.closest('.product-details-tab-container, .tab-content');
    var tabList = null;
    if (tabContent && tabContent.parentElement) {
      tabList = tabContent.parentElement.querySelector('ul.nav-tabs, ul.nav.nav-tabs');
    }
    if (!tabList && tabContent && tabContent.previousElementSibling) {
      var prev = tabContent.previousElementSibling;
      if (prev.classList && prev.classList.contains('responsive-tabs-sm-connector') && prev.previousElementSibling) {
        tabList = prev.previousElementSibling;
      } else if (prev.matches && prev.matches('ul.nav-tabs, ul.nav.nav-tabs')) {
        tabList = prev;
      }
    }
    var connector = tabContent && tabContent.parentElement
      ? tabContent.parentElement.querySelector('.responsive-tabs-sm-connector')
      : null;

    var anchor = tabList || tabContent;
    if (anchor && anchor.parentNode) {
      var slot = ensureSlotBefore(anchor);
      slot.appendChild(card);
    }

    card.dataset.rfrUnwrapped = '1';
    document.body.classList.add('rfr-ca-unwrapped');

    if (tabList) {
      var serverTabs = tabList.querySelectorAll('a[href="#domain"], a[href="#manage"]');
      for (var t = 0; t < serverTabs.length; t++) {
        var li = serverTabs[t].closest('li') || serverTabs[t];
        li.classList.add('rfr-ca-hide-tabs');
        hideEl(li);
      }
    }
    tabPane.classList.add('rfr-ca-hide-tabpane');
    hideEl(tabPane);

    var remaining = 0;
    if (tabList) {
      var items = tabList.querySelectorAll(':scope > li, :scope > .nav-item');
      for (var n = 0; n < items.length; n++) {
        if (items[n].style.display === 'none') continue;
        if (window.getComputedStyle(items[n]).display === 'none') continue;
        remaining++;
      }
    }
    if (remaining === 0) {
      if (tabList) {
        tabList.classList.add('rfr-ca-hide-tabs');
        hideEl(tabList);
      }
      if (connector) {
        connector.classList.add('rfr-ca-hide-tabs-connector');
        hideEl(connector);
      }
      if (tabContent) {
        tabContent.classList.add('rfr-ca-hide-tabpane');
        hideEl(tabContent);
      }
    } else if (tabContent) {
      tabContent.classList.add('rfr-ca-plain-tabs');
    }
  }

  function unwrapResellerClientArea() {
    var card = document.getElementById('rackflow-reseller-client-area');
    if (!card || card.dataset.rfrUnwrapped === '1') {
      return;
    }

    var isLagom = document.body.classList.contains('lagom')
      || !!card.closest('.panel-product-details')
      || !!document.getElementById('Overview');

    if (isLagom && unwrapLagom(card)) {
      return;
    }
    unwrapLegacy(card);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', unwrapResellerClientArea);
  } else {
    unwrapResellerClientArea();
  }
})();
</script>
HTML;
});

function rackflow_reseller_ensureModuleHooksRegistered()
{
    if (!class_exists('\Illuminate\Database\Capsule\Manager')) {
        return;
    }
    $capsule = '\Illuminate\Database\Capsule\Manager';
    $row = $capsule::table('tblconfiguration')->where('setting', 'ModuleHooks')->first();
    $current = $row ? trim((string)$row->value) : '';
    $parts = array_values(array_filter(array_map('trim', explode(',', $current))));
    if (in_array('rackflow_reseller', $parts, true)) {
        return;
    }
    $parts[] = 'rackflow_reseller';
    $value = implode(',', $parts);
    if ($row) {
        $capsule::table('tblconfiguration')->where('setting', 'ModuleHooks')->update(array(
            'value' => $value,
            'updated_at' => date('Y-m-d H:i:s'),
        ));
    } else {
        $capsule::table('tblconfiguration')->insert(array(
            'setting' => 'ModuleHooks',
            'value' => $value,
            'created_at' => date('Y-m-d H:i:s'),
            'updated_at' => date('Y-m-d H:i:s'),
        ));
    }
}

add_hook('AdminProductConfigFieldsSave', 1, function (array $vars) {
    $productId = !empty($vars['pid']) ? (int)$vars['pid'] : (!empty($_REQUEST['id']) ? (int)$_REQUEST['id'] : 0);
    if ($productId <= 0 || !class_exists('\Illuminate\Database\Capsule\Manager')) {
        return;
    }
    $product = \Illuminate\Database\Capsule\Manager::table('tblproducts')->where('id', $productId)->first();
    if (!$product || strtolower((string)$product->servertype) !== 'rackflow_reseller') {
        return;
    }
    rackflow_reseller_ensureModuleHooksRegistered();
    $options = isset($_REQUEST['packageconfigoption']) && is_array($_REQUEST['packageconfigoption'])
        ? $_REQUEST['packageconfigoption']
        : array();
    for ($index = 1; $index <= 8; $index++) {
        $flatName = 'packageconfigoption' . $index;
        if (!isset($options[$index]) && isset($_REQUEST[$flatName])) {
            $options[$index] = $_REQUEST[$flatName];
        }
    }
    $productCode = isset($options[2]) ? trim((string)$options[2]) : '';
    $enabled = rackflow_reseller_hookEnabled(isset($options[7]) ? $options[7] : '');
    $params = array('pid' => $productId);
    $serverId = rackflow_reseller_getProductServerId($productId);
    if ($serverId) {
        $params['serverid'] = $serverId;
    }
    $catalogProduct = $productCode !== '' ? rackflow_reseller_fetchProduct($params, $productCode) : array();
    rackflow_reseller_syncOsOption($productId, $catalogProduct, $enabled && !empty($catalogProduct));
    rackflow_reseller_ensureSshKeysCustomField($productId);
});
