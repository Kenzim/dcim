<?php
/**
 * RackFlow provisioning module hooks.
 *
 * Loaded by WHMCS from modules/servers/rackflow/hooks.php when the module
 * is active. If added after activation, open a RackFlow product Module
 * Settings tab and Save Changes once so WHMCS registers this file.
 *
 * AdminAreaFooterOutput: custom Module Settings UI on configproducts.php
 * for RackFlow products only (servertype check) that mirrors
 * packageconfigoption[1..8] (native WHMCS fields stay in the form for
 * submit; their table rows are hidden).
 *
 * AdminAreaFooterOutput: styles the admin service "Server overview" card on
 * clientsservices.php when #rackflow-admin-status is present.
 *
 * AdminProductConfigFieldsSave: syncs the order-form "Operating System"
 * configurable option when Customer OS Selection is enabled.
 *
 * ClientAreaFooterOutput: product-details cleanup for RackFlow (Twenty-One +
 * Lagom 2) — hide Configurable Options / Additional Information / Change
 * Password chrome, and lift the custom card without hiding Lagom #Overview.
 */

if (!defined('WHMCS')) {
    die('This file cannot be accessed directly');
}

require_once __DIR__ . '/rackflow.php';
require_once __DIR__ . '/git_update.php';

/**
 * Build catalog JSON + API readiness for the Module Settings UI.
 *
 * @param int $productId
 * @return array{products:array,error:?string,serverid:?int}
 */
function rackflow_hook_catalog_bootstrap($productId)
{
    $out = array(
        'products' => array(),
        'server_groups' => array(),
        'error' => null,
        'serverid' => null,
    );
    $serverId = rackflow_resolveModuleServerId($productId, array());
    if (!$serverId) {
        $out['error'] = 'Assign a WHMCS Server Group (Module Name row) so RackFlow can load the product catalog.';
        return $out;
    }
    $out['serverid'] = $serverId;
    $products = rackflow_fetchCatalogProducts(array('serverid' => $serverId), null);
    if (!$products) {
        $out['error'] = 'Could not load products from RackFlow. Check the server API URL and billing API key.';
        return $out;
    }
    $out['products'] = $products;
    $groups = rackflow_getServerGroups(array('serverid' => $serverId));
    $out['server_groups'] = is_array($groups) ? $groups : array();
    return $out;
}

/**
 * True when this product uses the RackFlow server module.
 *
 * Prefers a posted Module Name (servertype) on save / form submit so a
 * just-changed dropdown wins over the DB value; otherwise reads tblproducts.
 *
 * @param int $productId
 * @return bool
 */
function rackflow_hook_productIsRackflow($productId)
{
    if (isset($_REQUEST['servertype'])) {
        $posted = strtolower(trim((string)$_REQUEST['servertype']));
        if ($posted !== '') {
            return $posted === 'rackflow';
        }
    }
    $productId = (int)$productId;
    if ($productId <= 0 || !class_exists('\Illuminate\Database\Capsule\Manager')) {
        return false;
    }
    try {
        $product = \Illuminate\Database\Capsule\Manager::table('tblproducts')
            ->where('id', $productId)
            ->first();
        return $product && strtolower((string)$product->servertype) === 'rackflow';
    } catch (Exception $e) {
        return false;
    }
}

add_hook('AdminProductConfigFieldsSave', 1, function (array $vars) {
    $productId = 0;
    if (!empty($vars['pid'])) {
        $productId = (int)$vars['pid'];
    } elseif (!empty($_REQUEST['id'])) {
        $productId = (int)$_REQUEST['id'];
    }
    if ($productId <= 0) {
        return;
    }
    // Do not sync OS / hygiene for DCIMPC (or any other) module products.
    if (!rackflow_hook_productIsRackflow($productId)) {
        return;
    }

    $opts = array();
    if (isset($_REQUEST['packageconfigoption']) && is_array($_REQUEST['packageconfigoption'])) {
        $opts = $_REQUEST['packageconfigoption'];
    }
    // WHMCS may post packageconfigoption1..N instead of an array.
    for ($i = 1; $i <= 8; $i++) {
        if (!isset($opts[$i]) && isset($_REQUEST['packageconfigoption' . $i])) {
            $opts[$i] = $_REQUEST['packageconfigoption' . $i];
        }
    }

    $enabled = rackflow_isCustomerOsSelectionEnabled(isset($opts[7]) ? $opts[7] : '');
    $productCode = isset($opts[2]) ? trim((string)$opts[2]) : '';
    $serverId = rackflow_resolveModuleServerId($productId, array());
    if (!$serverId) {
        rackflow_log('OS sync skipped: no server', array('pid' => $productId));
        // Still surface product hygiene warnings when possible.
        rackflow_warnProductHygiene($productId);
        return;
    }

    $catalogProduct = array();
    if ($productCode !== '') {
        $apiConfig = rackflow_getApiConfig(array('serverid' => $serverId));
        if (!empty($apiConfig['url']) && !empty($apiConfig['key'])) {
            $result = rackflow_apiCall(
                $apiConfig['url'],
                $apiConfig['key'],
                'GET',
                '/api/billing/products/' . rawurlencode($productCode),
                null
            );
            if ($result['success'] && isset($result['data']) && is_array($result['data'])) {
                $catalogProduct = $result['data'];
            }
        }
    }

    $groupOsTemplates = array();
    $serverGroupId = isset($opts[4]) ? trim((string)$opts[4]) : '';
    if ($serverGroupId !== '') {
        $groupOsTemplates = rackflow_osTemplatesForServerGroupId(
            array('serverid' => $serverId, 'configoption4' => $serverGroupId),
            $serverGroupId
        );
    }

    rackflow_syncCheckoutOsOption(
        $productId,
        $catalogProduct,
        $enabled && !empty($catalogProduct),
        $groupOsTemplates
    );
    // Ensure checkout can collect multi-line SSH public keys for Linux templates.
    rackflow_ensureSshKeysCustomField($productId);
    rackflow_warnProductHygiene($productId);
});

/**
 * Cart configure-product: show SSH Public Keys custom field only when the
 * selected Operating System accepts SSH keys (rfvt: map from catalog).
 */
add_hook('ClientAreaFooterOutput', 2, function (array $vars) {
    $filename = isset($vars['filename']) ? (string)$vars['filename'] : '';
    $templatefile = isset($vars['templatefile']) ? (string)$vars['templatefile'] : '';
    $isCartConfigure = (
        $filename === 'cart'
        || strpos($templatefile, 'configureproduct') !== false
        || (!empty($_REQUEST['a']) && (string)$_REQUEST['a'] === 'confproduct')
    );
    if (!$isCartConfigure) {
        return '';
    }

    $productId = 0;
    if (!empty($_REQUEST['pid'])) {
        $productId = (int)$_REQUEST['pid'];
    } elseif (!empty($vars['productinfo']['pid'])) {
        $productId = (int)$vars['productinfo']['pid'];
    } elseif (!empty($vars['productinfo']['id'])) {
        $productId = (int)$vars['productinfo']['id'];
    }
    if ($productId <= 0) {
        return '';
    }

    // Only for RackFlow module products.
    try {
        if (!class_exists('\Illuminate\Database\Capsule\Manager')) {
            return '';
        }
        $product = \Illuminate\Database\Capsule\Manager::table('tblproducts')
            ->where('id', $productId)
            ->first();
        if (!$product || empty($product->servertype) || $product->servertype !== 'rackflow') {
            return '';
        }
    } catch (Exception $e) {
        return '';
    }

    rackflow_ensureSshKeysCustomField($productId);

    $tokenAcceptMap = array();
    $serverId = rackflow_resolveModuleServerId($productId, array());
    $productCode = '';
    if (!empty($product->configoption2)) {
        $productCode = trim((string)$product->configoption2);
    }
    if ($serverId && $productCode !== '') {
        $apiConfig = rackflow_getApiConfig(array('serverid' => $serverId));
        if (!empty($apiConfig['url']) && !empty($apiConfig['key'])) {
            $result = rackflow_apiCall(
                $apiConfig['url'],
                $apiConfig['key'],
                'GET',
                '/api/billing/products/' . rawurlencode($productCode),
                null
            );
            if (!empty($result['success']) && !empty($result['data']) && is_array($result['data'])) {
                $groupOsTemplates = array();
                if (!empty($product->configoption4)) {
                    $groupOsTemplates = rackflow_osTemplatesForServerGroupId(
                        array('serverid' => $serverId, 'configoption4' => $product->configoption4),
                        $product->configoption4
                    );
                }
                $tokenAcceptMap = rackflow_sshAcceptMapFromCatalog($result['data'], $groupOsTemplates);
            }
        }
    }

    $checkoutMap = rackflow_sshAcceptMapForCheckout($productId, $tokenAcceptMap);
    $acceptMap = isset($checkoutMap['map']) && is_array($checkoutMap['map'])
        ? $checkoutMap['map']
        : $tokenAcceptMap;
    $osConfigOptionId = !empty($checkoutMap['os_config_option_id'])
        ? (int)$checkoutMap['os_config_option_id']
        : 0;

    $mapJson = json_encode($acceptMap);
    if ($mapJson === false) {
        $mapJson = '{}';
    }
    $fieldNameJson = json_encode(RACKFLOW_SSH_KEYS_FIELD_NAME);
    $osConfigOptionIdJson = json_encode($osConfigOptionId);

    return <<<HTML
<script>
(function () {
  var acceptMap = {$mapJson};
  var fieldLabel = {$fieldNameJson};
  var osConfigOptionId = {$osConfigOptionIdJson};

  function tokenFromOsValue(raw) {
    if (!raw) { return ''; }
    var s = String(raw).trim();
    var pipe = s.indexOf('|');
    if (pipe > 0) { s = s.slice(0, pipe); }
    return s;
  }

  function osAccepts(raw) {
    var token = tokenFromOsValue(raw);
    if (!token) { return false; }
    if (Object.prototype.hasOwnProperty.call(acceptMap, token)) {
      return !!acceptMap[token];
    }
    // Unknown WHMCS sub-id / token: hide (Windows and other non-SSH OS must not show).
    return false;
  }

  function findSshFieldRow() {
    var labels = document.querySelectorAll('label, td, th, .field-name, .form-group label');
    for (var i = 0; i < labels.length; i++) {
      var t = (labels[i].textContent || '').replace(/\\s+/g, ' ').trim();
      if (t === fieldLabel || t.indexOf(fieldLabel) === 0) {
        return labels[i].closest('.form-group, .row, tr, fieldset, .panel-body > div') || labels[i].parentElement;
      }
    }
    // Fallback: textarea whose name looks like a custom field
    var areas = document.querySelectorAll('textarea');
    for (var j = 0; j < areas.length; j++) {
      var n = areas[j].getAttribute('name') || '';
      var ph = areas[j].getAttribute('placeholder') || '';
      if (/customfield/i.test(n) || /ssh/i.test(ph) || /ssh/i.test(n)) {
        // Prefer the one near SSH label already handled; skip generic
        var row = areas[j].closest('.form-group, .row, tr, fieldset');
        if (row && (row.textContent || '').indexOf('SSH') !== -1) {
          return row;
        }
      }
    }
    return null;
  }

  function selectHasMappedOption(s) {
    var opts = s.options || [];
    for (var o = 0; o < opts.length; o++) {
      var v = tokenFromOsValue(opts[o].value || '');
      if (v && Object.prototype.hasOwnProperty.call(acceptMap, v)) {
        return true;
      }
      var txt = tokenFromOsValue(opts[o].text || '');
      if (txt && (txt.indexOf('rfvt:') === 0 || txt.indexOf('rfos:') === 0)) {
        return true;
      }
    }
    return false;
  }

  function findOsSelect() {
    if (osConfigOptionId) {
      var byName = document.querySelector('select[name=\"configoption[' + osConfigOptionId + ']\"]');
      if (byName) { return byName; }
    }
    var selects = document.querySelectorAll('select');
    var labelMatch = null;
    for (var i = 0; i < selects.length; i++) {
      var s = selects[i];
      var name = (s.getAttribute('name') || '') + ' ' + (s.getAttribute('id') || '');
      var labelText = '';
      if (s.id) {
        var lab = document.querySelector('label[for=\"' + s.id + '\"]');
        if (lab) { labelText = lab.textContent || ''; }
      }
      var nearby = (s.closest('.form-group, tr, .row') || s.parentElement);
      if (nearby) {
        var nearLab = nearby.querySelector('label');
        if (nearLab) { labelText += ' ' + (nearLab.textContent || ''); }
      }
      var blob = (name + ' ' + labelText).toLowerCase().replace(/\\s+/g, ' ');
      var isOsLabel = blob.indexOf('operating system') !== -1 || /(^|[\\s>])os([\\s:<]|$)/.test(blob);
      if (isOsLabel && selectHasMappedOption(s)) {
        return s;
      }
      if (isOsLabel && !labelMatch) {
        labelMatch = s;
      }
      if (/configoption\\s*\\[/.test(name) && selectHasMappedOption(s)) {
        return s;
      }
    }
    if (labelMatch) { return labelMatch; }
    for (var k = 0; k < selects.length; k++) {
      if (selectHasMappedOption(selects[k])) {
        return selects[k];
      }
    }
    return null;
  }

  function findAdditionalInfoHeading() {
    var headings = document.querySelectorAll('.sub-heading');
    for (var i = 0; i < headings.length; i++) {
      if (/Additional Information/i.test(headings[i].textContent || '')) {
        return headings[i];
      }
    }
    return null;
  }

  function isVisibleField(el) {
    if (!el || el.type === 'hidden') { return false; }
    var wrap = el.closest('.form-group, .row, tr') || el;
    return getComputedStyle(wrap).display !== 'none'
      && getComputedStyle(el).display !== 'none'
      && getComputedStyle(el).visibility !== 'hidden';
  }

  function syncAdditionalInfoSection() {
    var heading = findAdditionalInfoHeading();
    if (!heading) { return; }
    var hasVisibleFields = false;
    var fieldContainers = [];
    var n = heading.nextElementSibling;
    while (n && !n.classList.contains('sub-heading')) {
      if (n.classList.contains('alert')) { break; }
      if (
        n.classList.contains('field-container')
        || n.querySelector('input:not([type=\"hidden\"]), textarea, select')
      ) {
        fieldContainers.push(n);
        var fields = n.querySelectorAll('input, textarea, select');
        for (var i = 0; i < fields.length; i++) {
          if (isVisibleField(fields[i])) {
            hasVisibleFields = true;
            break;
          }
        }
      }
      n = n.nextElementSibling;
    }
    heading.style.display = hasVisibleFields ? '' : 'none';
    for (var c = 0; c < fieldContainers.length; c++) {
      var cont = fieldContainers[c];
      if (!cont.classList.contains('field-container')) { continue; }
      var any = false;
      var fields2 = cont.querySelectorAll('input, textarea, select');
      for (var j = 0; j < fields2.length; j++) {
        if (isVisibleField(fields2[j])) { any = true; break; }
      }
      cont.style.display = any ? '' : 'none';
    }
  }

  function syncVisibility() {
    var row = findSshFieldRow();
    var os = findOsSelect();
    if (row) {
      // Fail-closed when OS select is known: only Linux/SSH-capable templates show.
      // Fail-open only if we cannot find the OS control at all (theme quirks).
      var show = os ? osAccepts(os.value) : true;
      row.style.display = show ? '' : 'none';
      if (!show) {
        var ta = row.querySelector('textarea, input');
        if (ta) { ta.value = ''; }
      }
    }
    syncAdditionalInfoSection();
  }

  function bind() {
    syncVisibility();
    var os = findOsSelect();
    if (os && !os._rfSshBound) {
      os._rfSshBound = true;
      os.addEventListener('change', syncVisibility);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bind);
  } else {
    bind();
  }
  // WHMCS themes sometimes re-render configurables asynchronously
  setTimeout(bind, 500);
  setTimeout(bind, 1500);
})();
</script>
HTML;
});

if (!class_exists('RackflowGitUpdateWidget', false) && class_exists('\WHMCS\Module\AbstractWidget')) {
    class RackflowGitUpdateWidget extends \WHMCS\Module\AbstractWidget
    {
        protected $title = 'RackFlow';
        protected $description = 'Git updates for the RackFlow provisioning module.';
        protected $weight = 150;
        protected $columns = 1;
        protected $cache = false;
        protected $requiredPermission = '';

        public function getData()
        {
            return array(
                'url' => rackflow_gitupdateAdminEntryUrl(),
                'status' => rackflow_gitupdate_statusPayload(),
            );
        }

        public function generateOutput($data)
        {
            $url = htmlspecialchars(
                isset($data['url']) ? (string)$data['url'] : rackflow_gitupdateAdminEntryUrl(),
                ENT_QUOTES,
                'UTF-8'
            );
            $status = isset($data['status']) && is_array($data['status']) ? $data['status'] : array();
            $sha = isset($status['last_sha']) ? (string)$status['last_sha'] : '';
            $when = isset($status['last_at']) ? (string)$status['last_at'] : '';
            if ($sha !== '') {
                $applied = htmlspecialchars(substr($sha, 0, 12), ENT_QUOTES, 'UTF-8');
                if ($when !== '') {
                    $applied .= ' · ' . htmlspecialchars($when, ENT_QUOTES, 'UTF-8');
                }
            } else {
                $applied = 'Never applied via git updates';
            }
            return '<div class="widget-content-padded">'
                . '<div class="text-muted" style="margin:0 0 12px;">Provisioning module files from git.</div>'
                . '<div style="margin:0 0 12px;font-size:12px;">Last applied: ' . $applied . '</div>'
                . '<a href="' . $url . '" class="btn btn-default btn-sm">'
                . '<i class="fas fa-arrow-right"></i> Git updates</a>'
                . '</div>';
        }
    }
}

add_hook('AdminHomeWidgets', 1, function () {
    if (!class_exists('RackflowGitUpdateWidget')) {
        return;
    }
    return new RackflowGitUpdateWidget();
});

add_hook('AdminAreaFooterOutput', 1, function (array $vars) {
    $filename = isset($vars['filename']) ? (string) $vars['filename'] : '';
    $uri = isset($_SERVER['REQUEST_URI']) ? (string) $_SERVER['REQUEST_URI'] : '';
    $onProducts = (
        $filename === 'configproducts'
        || strpos($uri, 'configproducts.php') !== false
    );
    if (!$onProducts) {
        return '';
    }

    $productId = isset($_REQUEST['id']) ? (int)$_REQUEST['id'] : 0;
    // Only inject the Module Settings mirror UI for RackFlow products.
    // Without this, any product with packageconfigoption[1] (e.g. DCIMPC)
    // would get the RackFlow panel overlaid on its native fields.
    if ($productId <= 0 || !rackflow_hook_productIsRackflow($productId)) {
        return '';
    }
    $bootstrap = rackflow_hook_catalog_bootstrap($productId);
    $catalogJson = json_encode(
        $bootstrap,
        JSON_HEX_TAG | JSON_HEX_AMP | JSON_HEX_APOS | JSON_HEX_QUOT
    );
    if ($catalogJson === false) {
        $catalogJson = '{"products":[],"error":"Failed to encode catalog","serverid":null}';
    }
    $gitUpdateUrlJson = json_encode(
        rackflow_gitupdateAdminEntryUrl(),
        JSON_HEX_TAG | JSON_HEX_AMP | JSON_HEX_APOS | JSON_HEX_QUOT
    );
    if ($gitUpdateUrlJson === false) {
        $gitUpdateUrlJson = '""';
    }

    $hygieneHtml = '';
    if (
        !empty($_SESSION['rackflow_product_hygiene_warnings'])
        && is_array($_SESSION['rackflow_product_hygiene_warnings'])
    ) {
        $flash = $_SESSION['rackflow_product_hygiene_warnings'];
        unset($_SESSION['rackflow_product_hygiene_warnings']);
        $flashPid = isset($flash['pid']) ? (int)$flash['pid'] : 0;
        if ($flashPid === $productId || $productId === 0) {
            $msgs = isset($flash['messages']) && is_array($flash['messages']) ? $flash['messages'] : array();
            if ($msgs) {
                $hygieneHtml = '<div class="rf-ms__hygiene" style="margin:12px 0;padding:12px 14px;'
                    . 'border:1px solid #f0d48a;background:#fff8e6;border-radius:8px;color:#5c4813;">'
                    . '<strong>RackFlow warnings</strong><ul style="margin:8px 0 0 18px;">';
                foreach ($msgs as $m) {
                    $hygieneHtml .= '<li>' . htmlspecialchars((string)$m, ENT_QUOTES, 'UTF-8') . '</li>';
                }
                $hygieneHtml .= '</ul></div>';
            }
        }
    }

    return <<<HTML
{$hygieneHtml}
<style id="rackflow-module-ui-css">
  .rf-ms {
    --rf-bg: #f4f6f8;
    --rf-card: #ffffff;
    --rf-ink: #1c2430;
    --rf-muted: #5b6775;
    --rf-line: #d5dce5;
    --rf-accent: #0f6e56;
    --rf-accent-soft: #e7f5ef;
    --rf-warn-bg: #fff8e6;
    --rf-warn-line: #f0d48a;
    --rf-radius: 12px;
    margin: 10px 0 18px;
    width: 100%;
    font-family: "Segoe UI", system-ui, -apple-system, sans-serif;
    color: var(--rf-ink);
  }
  tr.rf-ms-row > td {
    padding: 6px 0 8px !important;
    width: 100% !important;
  }
  .rf-ms *, .rf-ms *::before, .rf-ms *::after { box-sizing: border-box; }
  .rf-ms__card {
    background: var(--rf-card);
    border: 1px solid var(--rf-line);
    border-radius: var(--rf-radius);
    padding: 22px 24px 20px;
  }
  .rf-ms__head {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 24px;
    margin-bottom: 18px;
  }
  .rf-ms__title {
    margin: 0 0 4px;
    font-size: 18px;
    font-weight: 700;
    letter-spacing: -0.02em;
  }
  .rf-ms__lead {
    margin: 0;
    max-width: 62ch;
    font-size: 13px;
    color: var(--rf-muted);
    line-height: 1.45;
  }
  .rf-ms__git {
    display: inline-flex;
    align-items: center;
    height: 34px;
    padding: 0 12px;
    border: 1px solid var(--rf-line);
    border-radius: 8px;
    background: #fff;
    color: var(--rf-ink);
    font-size: 12px;
    font-weight: 650;
    text-decoration: none;
    white-space: nowrap;
  }
  .rf-ms__git:hover { border-color: var(--rf-accent); color: var(--rf-accent); }
  .rf-ms__seg {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 12px;
    margin-bottom: 20px;
  }
  .rf-ms__seg input { position: absolute; opacity: 0; pointer-events: none; }
  .rf-ms__seg label {
    display: flex;
    flex-direction: column;
    justify-content: flex-start;
    min-height: 96px;
    margin: 0;
    padding: 14px 16px;
    border: 1px solid var(--rf-line);
    border-radius: 10px;
    background: var(--rf-bg);
    cursor: pointer;
    text-align: left;
    transition: border-color .15s, background .15s, box-shadow .15s;
  }
  .rf-ms__seg label:hover {
    border-color: #b7c2cf;
    background: #fff;
  }
  .rf-ms__seg input:checked + label {
    border-color: var(--rf-accent);
    background: var(--rf-accent-soft);
    box-shadow: 0 0 0 1px var(--rf-accent);
  }
  .rf-ms__seg strong { font-size: 14px; margin-bottom: 6px; }
  .rf-ms__seg .rf-desc { font-size: 12px; color: var(--rf-muted); line-height: 1.4; }
  .rf-ms__grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 14px 18px;
  }
  .rf-ms__field label {
    display: block;
    margin: 0 0 6px;
    font-size: 12px;
    font-weight: 650;
  }
  .rf-ms__field input[type="text"],
  .rf-ms__field select {
    width: 100%;
    height: 36px;
    padding: 6px 10px;
    border: 1px solid var(--rf-line);
    border-radius: 8px;
    background: #fff;
    font-size: 13px;
  }
  .rf-ms__field input[type="text"]:focus,
  .rf-ms__field select:focus {
    outline: none;
    border-color: var(--rf-accent);
    box-shadow: 0 0 0 3px rgba(15, 110, 86, 0.14);
  }
  .rf-ms__field .rf-help {
    margin: 6px 0 0;
    font-size: 11px;
    color: var(--rf-muted);
    line-height: 1.4;
  }
  .rf-ms__panel {
    border-top: 1px solid var(--rf-line);
    margin-top: 18px;
    padding-top: 16px;
  }
  .rf-ms__panel-title {
    margin: 0 0 12px;
    font-size: 11px;
    font-weight: 750;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--rf-muted);
  }
  .rf-ms__preview {
    margin-top: 16px;
    padding: 14px 16px;
    border: 1px solid var(--rf-line);
    border-radius: 10px;
    background: var(--rf-bg);
  }
  .rf-ms__preview[hidden] { display: none !important; }
  .rf-ms__preview h4 {
    margin: 0 0 8px;
    font-size: 14px;
    font-weight: 700;
  }
  .rf-ms__preview-meta {
    margin: 0 0 12px;
    font-size: 12px;
    color: var(--rf-muted);
    line-height: 1.45;
  }
  .rf-ms__preview-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 12px;
  }
  .rf-ms__preview-block {
    background: #fff;
    border: 1px solid var(--rf-line);
    border-radius: 8px;
    padding: 10px 12px;
    min-height: 88px;
  }
  .rf-ms__preview-block h5 {
    margin: 0 0 8px;
    font-size: 11px;
    font-weight: 750;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--rf-muted);
  }
  .rf-ms__preview-block ul {
    margin: 0;
    padding: 0;
    list-style: none;
  }
  .rf-ms__preview-block li {
    font-size: 12px;
    line-height: 1.45;
    padding: 2px 0;
    word-break: break-word;
  }
  .rf-ms__preview-block li span {
    color: var(--rf-muted);
  }
  .rf-ms__check {
    display: flex;
    gap: 10px;
    align-items: flex-start;
    margin-top: 4px;
    padding: 12px 14px;
    border: 1px solid var(--rf-line);
    border-radius: 10px;
    background: var(--rf-bg);
  }
  .rf-ms__check input { margin-top: 2px; }
  .rf-ms__check label { margin: 0; font-size: 13px; font-weight: 650; cursor: pointer; }
  .rf-ms__check p { margin: 4px 0 0; font-size: 11px; color: var(--rf-muted); line-height: 1.4; }
  .rf-ms__banner {
    margin: 0 0 14px;
    padding: 10px 12px;
    border: 1px solid var(--rf-warn-line);
    border-radius: 8px;
    background: var(--rf-warn-bg);
    font-size: 12px;
    line-height: 1.4;
  }
  .rf-ms__hidden-native { display: none !important; }
  @media (max-width: 900px) {
    .rf-ms__seg,
    .rf-ms__grid,
    .rf-ms__preview-grid { grid-template-columns: 1fr; }
    .rf-ms__head { flex-direction: column; align-items: flex-start; }
  }
</style>
<script type="text/javascript">
(function () {
  // ConfigOptions order in rackflow_ConfigOptions():
  // 1 Service Type, 2 Product Code, 3 OS Code, 4 Server Group,
  // 5 Proxmox Location, 6 Proxmox Node, 7 Customer OS Selection,
  // 8 Allow Client Portal Sign-In (Yes/No)
  var IDX = {
    serviceType: 1,
    productCode: 2,
    osCode: 3,
    serverGroup: 4,
    proxmoxLocation: 5,
    proxmoxNode: 6,
    customerOs: 7,
    portalSignIn: 8
  };

  var UI_ID = 'rackflow-module-ui';
  var CATALOG = {$catalogJson};
  var GIT_UPDATE_URL = {$gitUpdateUrlJson};
  var applyTimer = null;
  var observing = false;

  function jq() {
    return (typeof window.jQuery !== 'undefined') ? window.jQuery : null;
  }

  function optionSelector(index) {
    return '[name="packageconfigoption[' + index + ']"], [name="packageconfigoption' + index + '"]';
  }

  /** Module Name dropdown on configproducts — must stay rackflow while mounted. */
  function selectedModuleType() {
    var \$ = jq();
    if (!\$) return '';
    var \$sel = \$('select[name="servertype"], select#servertype, #inputServerType').first();
    if (!\$sel.length) return '';
    return String(\$sel.val() || '').toLowerCase();
  }

  function isRackflowModuleContext() {
    var mod = selectedModuleType();
    // PHP only injects for RackFlow products; if the select is missing, allow mount.
    if (!mod) return true;
    return mod === 'rackflow';
  }

  function teardownUi(\$) {
    var el = document.getElementById(UI_ID);
    if (el) {
      var \$row = \$(el).closest('tr.rf-ms-row');
      if (\$row.length) {
        \$row.remove();
      } else {
        \$(el).remove();
      }
    }
    \$('.rf-ms__hidden-native').removeClass('rf-ms__hidden-native');
  }

  function nativeControl(index) {
    var \$ = jq();
    var \$all = \$(optionSelector(index));
    if (!\$all.length) return \$all;
    // WHMCS yesno fields often render as hidden "" + checkbox "on".
    // Prefer a visible/meaningful control over the empty hidden sibling.
    var \$box = \$all.filter(':checkbox').first();
    if (\$box.length) return \$box;
    var \$select = \$all.filter('select').first();
    if (\$select.length) return \$select;
    var \$text = \$all.filter('input[type="text"], input:not([type]), textarea').first();
    if (\$text.length) return \$text;
    return \$all.first();
  }

  function hideNativePackageRows(\$) {
    var seen = [];
    \$('[name^="packageconfigoption"]').each(function () {
      var \$el = \$(this);
      var \$tr = \$el.closest('tr');
      if (\$tr.length) {
        var node = \$tr.get(0);
        if (seen.indexOf(node) !== -1) return;
        seen.push(node);
        \$tr.addClass('rf-ms__hidden-native');
      } else {
        \$el.closest('.form-group, .row').addClass('rf-ms__hidden-native');
      }
    });
    \$('#mode-switch').addClass('rf-ms__hidden-native');
    \$('#mode-switch').closest('tr, p, .form-group').addClass('rf-ms__hidden-native');
  }

  function copySelectOptions(\$from, \$to) {
    if (!\$from.length || !\$to.length || \$from.prop('tagName') !== 'SELECT') return;
    \$to.empty();
    \$from.find('option').each(function () {
      var \$o = \$(this);
      \$to.append(
        \$('<option/>')
          .attr('value', \$o.attr('value'))
          .prop('selected', \$o.prop('selected'))
          .text(\$o.text())
      );
    });
    \$to.val(\$from.val());
  }

  function productsForType(serviceType) {
    var all = (CATALOG && CATALOG.products) ? CATALOG.products : [];
    var st = String(serviceType || '').toLowerCase();
    return all.filter(function (p) {
      if (!p || !p.code) return false;
      if (!st) return true;
      return String(p.service_type || '').toLowerCase() === st;
    });
  }

  function findProduct(code) {
    var all = (CATALOG && CATALOG.products) ? CATALOG.products : [];
    for (var i = 0; i < all.length; i++) {
      if (String(all[i].code) === String(code)) return all[i];
    }
    return null;
  }

  function fillProductSelect(\$ui, serviceType, selected) {
    var \$sel = \$ui.find('[data-rf-field="productCode"]');
    var list = productsForType(serviceType);
    \$sel.empty();
    \$sel.append(\$('<option/>').attr('value', '').text('— Select a RackFlow product —'));
    list.forEach(function (p) {
      var label = (p.name || p.code) + ' (' + p.code + ')';
      \$sel.append(\$('<option/>').attr('value', p.code).text(label));
    });
    if (selected && \$sel.find('option[value="' + selected.replace(/"/g, '\\\\"') + '"]').length) {
      \$sel.val(selected);
    } else if (selected) {
      // Keep a saved code even if it is missing from the filtered list.
      \$sel.append(\$('<option/>').attr('value', selected).text(selected + ' (saved)'));
      \$sel.val(selected);
    }
  }

  function findServerGroup(id) {
    var all = (CATALOG && CATALOG.server_groups) ? CATALOG.server_groups : [];
    var want = String(id || '');
    for (var i = 0; i < all.length; i++) {
      if (String(all[i].id) === want) return all[i];
    }
    return null;
  }

  function fillOsSelect(\$ui, product, selected) {
    var \$sel = \$ui.find('[data-rf-field="osCode"]');
    \$sel.empty();
    \$sel.append(\$('<option/>').attr('value', '').text('— None / product default —'));
    var st = \$ui.find('input[name="rf_service_type"]:checked').val() || 'bare_metal';
    if (st === 'bare_metal') {
      var group = findServerGroup(\$ui.find('[data-rf-field="serverGroup"]').val() || '');
      var templates = (group && group.os_templates) ? group.os_templates : [];
      templates.forEach(function (t) {
        if (!t.id) return;
        \$sel.append(\$('<option/>').attr('value', t.id).text(t.name || t.id));
      });
    } else if (product) {
      var profiles = product.os_profiles || [];
      profiles.forEach(function (os) {
        if (!os.code) return;
        \$sel.append(\$('<option/>').attr('value', os.code).text(os.name || os.code));
      });
    }
    if (selected) {
      if (!\$sel.find('option[value="' + selected.replace(/"/g, '\\\\"') + '"]').length) {
        \$sel.append(\$('<option/>').attr('value', selected).text(selected + ' (saved)'));
      }
      \$sel.val(selected);
    }
  }

  function specEntries(specs) {
    var out = [];
    if (!specs || typeof specs !== 'object') return out;
    var prefer = ['cpu_cores', 'cores', 'ram_mb', 'memory_mb', 'disk_gb', 'disk_size_gb', 'sockets', 'cpu_count'];
    var seen = {};
    prefer.forEach(function (k) {
      if (specs[k] === undefined || specs[k] === null || specs[k] === '') return;
      seen[k] = true;
      out.push({ key: k, value: specs[k] });
    });
    Object.keys(specs).forEach(function (k) {
      if (seen[k]) return;
      var v = specs[k];
      if (v === null || v === undefined || v === '' || typeof v === 'object') return;
      out.push({ key: k, value: v });
    });
    return out.slice(0, 8);
  }

  function renderPreview(\$ui, product) {
    var \$box = \$ui.find('[data-rf-preview]');
    if (!product) {
      \$box.attr('hidden', 'hidden');
      return;
    }
    \$box.removeAttr('hidden');
    \$ui.find('[data-rf-preview-title]').text(product.name || product.code);
    var meta = [];
    meta.push('Code: ' + product.code);
    if (product.family && product.family.name) {
      meta.push('Family: ' + product.family.name);
    }
    if (product.description) meta.push(product.description);
    \$ui.find('[data-rf-preview-meta]').text(meta.join(' · '));

    var specs = specEntries(product.effective_specs || {});
    var \$specs = \$ui.find('[data-rf-preview-specs]');
    \$specs.empty();
    if (!specs.length) {
      \$specs.append('<li><span>No sizing set on this product.</span></li>');
    } else {
      specs.forEach(function (row) {
        var \$li = \$('<li/>');
        \$li.append(\$('<strong/>').text(row.key));
        \$li.append(document.createTextNode(': ' + String(row.value)));
        \$specs.append(\$li);
      });
    }

    var \$tmpl = \$ui.find('[data-rf-preview-templates]');
    \$tmpl.empty();
    var templates = product.vm_templates || [];
    if (!templates.length) {
      \$tmpl.append('<li><span>No VM templates linked to this product.</span></li>');
    } else {
      templates.forEach(function (t) {
        \$tmpl.append(\$('<li/>').text(t.name || ('Template #' + t.id)));
      });
    }

    var mode = product.checkout_os_mode || 'none';
    var modeLabel = 'Checkout OS: none available for this product.';
    if (mode === 'vm_template') modeLabel = 'Checkout OS will list the linked VM templates.';
    else if (mode === 'server_group') modeLabel = 'Checkout OS will list OS templates from the selected server group.';
    else if (mode === 'os_profile') modeLabel = 'Checkout OS will list the linked OS choices.';
    \$ui.find('[data-rf-preview-mode]').text(modeLabel);
  }

  function nativeCustomerOsChecked() {
    var \$ = jq();
    var \$all = \$(optionSelector(IDX.customerOs));
    if (!\$all.length) return false;
    var \$box = \$all.filter(':checkbox');
    if (\$box.length) {
      return \$box.is(':checked');
    }
    var on = false;
    \$all.each(function () {
      var v = String(\$(this).val() || '').toLowerCase();
      if (v === 'on' || v === '1' || v === 'yes' || v === 'true') on = true;
    });
    return on;
  }

  function setNativeCustomerOs(checked) {
    var \$ = jq();
    var \$all = \$(optionSelector(IDX.customerOs));
    if (!\$all.length) return;
    var \$box = \$all.filter(':checkbox');
    if (\$box.length) {
      \$box.prop('checked', !!checked);
      // Clear companion hidden input so an unchecked box does not submit "".
      if (!checked) {
        \$all.filter('input[type="hidden"]').val('');
      }
      return;
    }
    \$all.first().val(checked ? 'on' : '');
  }

  function nativePortalSignInEnabled() {
    var \$ = jq();
    var \$all = \$(optionSelector(IDX.portalSignIn));
    if (!\$all.length) return true;
    var raw = String(nativeControl(IDX.portalSignIn).val() || '').toLowerCase();
    if (!raw) return true; // legacy / unset → allow
    return !(raw === 'no' || raw === 'off' || raw === 'false' || raw === '0');
  }

  function setNativePortalSignIn(enabled) {
    var \$ = jq();
    var \$native = nativeControl(IDX.portalSignIn);
    if (!\$native.length) return;
    var val = enabled ? 'Yes' : 'No';
    if (\$native.prop('tagName') === 'SELECT') {
      if (!\$native.find('option[value="' + val + '"]').length) {
        // Some WHMCS builds use lowercase option values.
        var lower = val.toLowerCase();
        if (\$native.find('option[value="' + lower + '"]').length) {
          val = lower;
        } else {
          \$native.append(\$('<option/>').attr('value', val).text(val));
        }
      }
    }
    if (\$native.val() !== val) {
      \$native.val(val);
    }
  }

  function syncFromNative(\$ui) {
    var \$st = nativeControl(IDX.serviceType);
    var st = String(\$st.val() || 'bare_metal').toLowerCase();
    if (!\$ui.find('input[name="rf_service_type"][value="' + st + '"]').length) {
      st = 'bare_metal';
    }
    \$ui.find('input[name="rf_service_type"][value="' + st + '"]').prop('checked', true);

    var productCode = nativeControl(IDX.productCode).val() || '';
    var osCode = nativeControl(IDX.osCode).val() || '';
    fillProductSelect(\$ui, st, productCode);
    \$ui.find('[data-rf-field="proxmoxNode"]').val(nativeControl(IDX.proxmoxNode).val() || '');
    copySelectOptions(nativeControl(IDX.serverGroup), \$ui.find('[data-rf-field="serverGroup"]'));
    copySelectOptions(nativeControl(IDX.proxmoxLocation), \$ui.find('[data-rf-field="proxmoxLocation"]'));
    fillOsSelect(\$ui, findProduct(productCode), osCode);
    renderPreview(\$ui, findProduct(productCode));
    \$ui.find('[data-rf-field="customerOs"]').prop('checked', nativeCustomerOsChecked());
    \$ui.find('[data-rf-field="portalSignIn"]').prop('checked', nativePortalSignInEnabled());
  }

  function syncToNative(\$ui) {
    var st = \$ui.find('input[name="rf_service_type"]:checked').val() || 'bare_metal';
    var \$st = nativeControl(IDX.serviceType);
    if (\$st.length && \$st.val() !== st) {
      \$st.val(st).trigger('change');
    }

    var map = {
      productCode: IDX.productCode,
      osCode: IDX.osCode,
      serverGroup: IDX.serverGroup,
      proxmoxLocation: IDX.proxmoxLocation,
      proxmoxNode: IDX.proxmoxNode
    };
    Object.keys(map).forEach(function (key) {
      var \$native = nativeControl(map[key]);
      var \$custom = \$ui.find('[data-rf-field="' + key + '"]');
      if (!\$native.length || !\$custom.length) return;
      var val = \$custom.val();
      // Native product loader may be a <select>; ensure the option exists.
      if (\$native.prop('tagName') === 'SELECT' && val && !\$native.find('option[value="' + String(val).replace(/"/g, '\\\\"') + '"]').length) {
        \$native.append(\$('<option/>').attr('value', val).text(val));
      }
      if (\$native.val() !== val) {
        \$native.val(val);
      }
    });
    setNativeCustomerOs(\$ui.find('[data-rf-field="customerOs"]').is(':checked'));
    setNativePortalSignIn(\$ui.find('[data-rf-field="portalSignIn"]').is(':checked'));
  }

  function applyPanels(\$ui) {
    var st = \$ui.find('input[name="rf_service_type"]:checked').val() || 'bare_metal';
    var isVm = (st === 'vm');
    var isProxy = (st === 'http_proxy');
    \$ui.find('[data-rf-panel="vm"]').toggle(isVm);
    \$ui.find('[data-rf-panel="bm"]').toggle(!isVm);
    // Default OS is bare-metal/legacy; VM installs come from linked templates,
    // and http_proxy has no OS to install at all.
    \$ui.find('[data-rf-field-wrap="osCode"]').toggle(!isVm && !isProxy);
    // The "bm" panel's Server Group field is required for bare metal but only
    // a legacy/optional override for http_proxy (which normally provisions
    // straight from the product's IPAM subnet, no server group needed).
    \$ui.find('[data-rf-panel="bm"] .rf-ms__panel-title').text(isProxy ? 'HTTP proxy (legacy hardware-bound)' : 'Bare metal');
    \$ui.find('[data-rf-panel="bm"] [data-rf-field-wrap="serverGroup"] .rf-help').text(
      isProxy
        ? 'Optional — only for a proxy bound to a physical server. Leave blank for the normal IPAM-based proxy (IP(s) come from the Product Code\\'s subnet).'
        : 'Controls permitted ISOs, scripts, and OS templates.'
    );
  }

  function ensureUi(\$) {
    if (document.getElementById(UI_ID)) return \$(document.getElementById(UI_ID));
    if (!nativeControl(IDX.serviceType).length) return \$();

    var banner = '';
    if (CATALOG && CATALOG.error) {
      banner = '<div class="rf-ms__banner">' + String(CATALOG.error).replace(/</g, '&lt;') + '</div>';
    }

    var html = ''
      + '<div id="' + UI_ID + '" class="rf-ms">'
      + '  <div class="rf-ms__card">'
      + '    <div class="rf-ms__head">'
      + '      <div>'
      + '        <h3 class="rf-ms__title">RackFlow settings</h3>'
      + '        <p class="rf-ms__lead">Pick a RackFlow catalog product. Specs and linked VM templates are loaded from RackFlow.</p>'
      + '      </div>'
      + '      <a class="rf-ms__git" href="' + GIT_UPDATE_URL + '">Git updates</a>'
      + '    </div>'
      + banner
      + '    <div class="rf-ms__seg" role="radiogroup" aria-label="Service type">'
      + '      <input type="radio" name="rf_service_type" id="rf_st_bm" value="bare_metal"/>'
      + '      <label for="rf_st_bm"><strong>Bare metal</strong><span class="rf-desc">Physical / IPMI servers via a RackFlow server group.</span></label>'
      + '      <input type="radio" name="rf_service_type" id="rf_st_vm" value="vm"/>'
      + '      <label for="rf_st_vm"><strong>VM</strong><span class="rf-desc">Proxmox guests from a catalog product / template.</span></label>'
      + '      <input type="radio" name="rf_service_type" id="rf_st_proxy" value="http_proxy"/>'
      + '      <label for="rf_st_proxy"><strong>HTTP proxy</strong><span class="rf-desc">HTTP/SOCKS5 proxy — IP(s) auto-assigned from IPAM.</span></label>'
      + '    </div>'
      + '    <div class="rf-ms__grid">'
      + '      <div class="rf-ms__field">'
      + '        <label for="rf_product_code">RackFlow product</label>'
      + '        <select id="rf_product_code" data-rf-field="productCode"></select>'
      + '        <p class="rf-help">Catalog product this WHMCS package provisions (plan + templates).</p>'
      + '      </div>'
      + '      <div class="rf-ms__field" data-rf-field-wrap="osCode">'
      + '        <label for="rf_os_code">Default OS template <span style="font-weight:500;color:var(--rf-muted)">(optional)</span></label>'
      + '        <select id="rf_os_code" data-rf-field="osCode"></select>'
      + '        <p class="rf-help">Bare metal only: default installer from the selected server group when the customer does not choose one at checkout.</p>'
      + '      </div>'
      + '    </div>'
      + '    <div class="rf-ms__preview" data-rf-preview hidden>'
      + '      <h4 data-rf-preview-title></h4>'
      + '      <p class="rf-ms__preview-meta" data-rf-preview-meta></p>'
      + '      <div class="rf-ms__preview-grid">'
      + '        <div class="rf-ms__preview-block"><h5>Specs</h5><ul data-rf-preview-specs></ul></div>'
      + '        <div class="rf-ms__preview-block"><h5>VM templates</h5><ul data-rf-preview-templates></ul></div>'
      + '      </div>'
      + '      <p class="rf-help" style="margin-top:10px" data-rf-preview-mode></p>'
      + '    </div>'
      + '    <div class="rf-ms__panel">'
      + '      <p class="rf-ms__panel-title">Checkout &amp; client area</p>'
      + '      <div class="rf-ms__check">'
      + '        <input type="checkbox" id="rf_customer_os" data-rf-field="customerOs"/>'
      + '        <div>'
          + '          <label for="rf_customer_os">Let customers choose OS at checkout</label>'
          + '          <p>On Save, syncs a WHMCS <strong>OS</strong> option from this product’s VM templates or the selected server group’s OS templates. Turn off to hide it.</p>'
      + '        </div>'
      + '      </div>'
      + '      <div class="rf-ms__check" style="margin-top:10px">'
      + '        <input type="checkbox" id="rf_portal_signin" data-rf-field="portalSignIn"/>'
      + '        <div>'
      + '          <label for="rf_portal_signin">Allow RackFlow portal sign-in</label>'
      + '          <p>Shows a one-click <strong>Open portal</strong> button on the client product page. Turn off to hide it for this product.</p>'
      + '        </div>'
      + '      </div>'
      + '    </div>'
      + '    <div class="rf-ms__panel" data-rf-panel="bm">'
      + '      <p class="rf-ms__panel-title">Bare metal / proxy</p>'
      + '      <div class="rf-ms__grid">'
      + '        <div class="rf-ms__field" data-rf-field-wrap="serverGroup">'
      + '          <label for="rf_server_group">Server group</label>'
      + '          <select id="rf_server_group" data-rf-field="serverGroup"></select>'
      + '          <p class="rf-help">Controls permitted ISOs, scripts, and OS templates.</p>'
      + '        </div>'
      + '      </div>'
      + '    </div>'
      + '    <div class="rf-ms__panel" data-rf-panel="vm">'
      + '      <p class="rf-ms__panel-title">Virtual machine</p>'
      + '      <div class="rf-ms__grid">'
      + '        <div class="rf-ms__field">'
      + '          <label for="rf_location">Proxmox location</label>'
      + '          <select id="rf_location" data-rf-field="proxmoxLocation"></select>'
      + '          <p class="rf-help">Default cluster. Order-form Location can override.</p>'
      + '        </div>'
      + '        <div class="rf-ms__field">'
      + '          <label for="rf_node">Proxmox node <span style="font-weight:500;color:var(--rf-muted)">(optional)</span></label>'
      + '          <input type="text" id="rf_node" data-rf-field="proxmoxNode" autocomplete="off" spellcheck="false" placeholder="e.g. epyc"/>'
      + '          <p class="rf-help">Pin a node, or leave blank to auto-pick.</p>'
      + '        </div>'
      + '      </div>'
      + '    </div>'
      + '  </div>'
      + '</div>';

    var \$ui = \$(html);
    var \$pkgTr = nativeControl(IDX.serviceType).closest('tr');
    var \$table = \$pkgTr.closest('table');
    if (\$table.length && \$pkgTr.length) {
      var colCount = 0;
      \$table.find('tr').first().children('td,th').each(function () {
        colCount += parseInt(\$(this).attr('colspan') || '1', 10);
      });
      if (!colCount) colCount = 4;
      var \$row = \$('<tr class="rf-ms-row"/>');
      var \$cell = \$('<td/>').attr('colspan', String(colCount));
      \$cell.append(\$ui);
      \$row.append(\$cell);
      \$pkgTr.before(\$row);
    } else {
      var \$tab = \$('#tabModuleSettings, .tab-pane.active, form').first();
      \$tab.prepend(\$ui);
    }
    return \$ui;
  }

  function onProductChanged(\$ui) {
    var code = \$ui.find('[data-rf-field="productCode"]').val() || '';
    var product = findProduct(code);
    var prevOs = \$ui.find('[data-rf-field="osCode"]').val() || '';
    fillOsSelect(\$ui, product, prevOs);
    renderPreview(\$ui, product);
    syncToNative(\$ui);
  }

  function bindUi(\$ui) {
    \$ui.off('.rfms');
    \$ui.on('change.rfms', 'input[name="rf_service_type"]', function () {
      var st = \$ui.find('input[name="rf_service_type"]:checked').val() || 'bare_metal';
      var current = \$ui.find('[data-rf-field="productCode"]').val() || '';
      fillProductSelect(\$ui, st, current);
      onProductChanged(\$ui);
      applyPanels(\$ui);
      syncToNative(\$ui);
    });
    \$ui.on('change.rfms', '[data-rf-field="productCode"]', function () {
      onProductChanged(\$ui);
    });
    \$ui.on('change.rfms', '[data-rf-field="serverGroup"]', function () {
      var code = \$ui.find('[data-rf-field="productCode"]').val() || '';
      var prevOs = \$ui.find('[data-rf-field="osCode"]').val() || '';
      fillOsSelect(\$ui, findProduct(code), prevOs);
      renderPreview(\$ui, findProduct(code));
      syncToNative(\$ui);
    });
    \$ui.on('input.rfms change.rfms', '[data-rf-field]', function () {
      syncToNative(\$ui);
    });
  }

  function mount() {
    var \$ = jq();
    if (!\$) return;
    if (!isRackflowModuleContext()) {
      teardownUi(\$);
      return;
    }
    if (!nativeControl(IDX.serviceType).length) return;

    hideNativePackageRows(\$);
    var \$ui = ensureUi(\$);
    if (!\$ui.length) return;

    syncFromNative(\$ui);
    applyPanels(\$ui);
    bindUi(\$ui);
  }

  function scheduleMount() {
    if (applyTimer) clearTimeout(applyTimer);
    applyTimer = setTimeout(mount, 80);
  }

  function boot() {
    var \$ = jq();
    if (!\$) {
      setTimeout(boot, 200);
      return;
    }
    \$(function () {
      scheduleMount();
      \$(document).on(
        'click.rfms',
        'a[href*="Module"], a[data-toggle="tab"], .nav-tabs a, #mode-switch',
        function () { setTimeout(scheduleMount, 200); }
      );
      \$(document).on(
        'change.rfms',
        'select[name="servertype"], select#servertype, #inputServerType',
        function () { setTimeout(scheduleMount, 200); }
      );
      \$(window).on('hashchange.rfms', function () { setTimeout(scheduleMount, 200); });
      if (!observing && typeof MutationObserver !== 'undefined') {
        observing = true;
        var obs = new MutationObserver(function (mutations) {
          for (var i = 0; i < mutations.length; i++) {
            var nodes = mutations[i].addedNodes;
            if (!nodes || !nodes.length) continue;
            for (var j = 0; j < nodes.length; j++) {
              var n = nodes[j];
              if (n.nodeType !== 1) continue;
              if (n.id === UI_ID || (n.querySelector && n.querySelector('#' + UI_ID))) continue;
              if (
                (n.matches && n.matches('[name^="packageconfigoption"]')) ||
                (n.querySelector && n.querySelector('[name^="packageconfigoption"]'))
              ) {
                scheduleMount();
                return;
              }
            }
          }
        });
        obs.observe(document.body, { childList: true, subtree: true });
      }
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
</script>
HTML;
});

/**
 * Hide the WHMCS "Actions" sidebar (Change Password / Power / …) for RackFlow
 * services — those controls live in clientarea.tpl instead.
 */
add_hook('ClientAreaPrimarySidebar', 1, function ($primarySidebar) {
    if (!$primarySidebar || !is_object($primarySidebar) || !method_exists($primarySidebar, 'getChild')) {
        return;
    }
    if (!rackflow_hook_isRackflowProductDetailsRequest()) {
        return;
    }
    if (!is_null($primarySidebar->getChild('Service Details Actions'))) {
        $primarySidebar->removeChild('Service Details Actions');
    }
});

/**
 * @return bool
 */
function rackflow_hook_isRackflowProductDetailsRequest()
{
    $action = isset($_GET['action']) ? (string)$_GET['action'] : '';
    if ($action !== 'productdetails') {
        return false;
    }
    $serviceId = isset($_REQUEST['id']) ? (int)$_REQUEST['id'] : 0;
    if ($serviceId <= 0 || !class_exists('\Illuminate\Database\Capsule\Manager')) {
        return false;
    }
    try {
        $hosting = \Illuminate\Database\Capsule\Manager::table('tblhosting')
            ->where('id', $serviceId)
            ->first();
        if (!$hosting) {
            return false;
        }
        $product = \Illuminate\Database\Capsule\Manager::table('tblproducts')
            ->where('id', (int)$hosting->packageid)
            ->first();
        return $product && strtolower((string)$product->servertype) === 'rackflow';
    } catch (\Throwable $e) {
        return false;
    }
}

/**
 * Strip configurable options and custom fields from the product-details page
 * for RackFlow so the theme never renders those tabs. Order-form options and
 * custom fields at checkout are unaffected.
 */
add_hook('ClientAreaPage', 1, function (array $vars) {
    $templateFile = isset($vars['templatefile']) ? (string)$vars['templatefile'] : '';
    $action = isset($_GET['action']) ? (string)$_GET['action'] : '';
    $onProductDetails = ($templateFile === 'clientareaproductdetails' || $action === 'productdetails');
    if (!$onProductDetails) {
        return array();
    }

    $module = '';
    if (!empty($vars['module'])) {
        $module = strtolower((string)$vars['module']);
    } elseif (!empty($vars['servertype'])) {
        $module = strtolower((string)$vars['servertype']);
    }
    if ($module !== 'rackflow') {
        return array();
    }

    return array(
        'configurableoptions' => array(),
        'customfields' => array(),
    );
});

/**
 * Product-details UI cleanup for RackFlow:
 * - hide Configurable Options / Additional Information / Change Password tabs
 * - hide WHMCS hostname/IP rows (Twenty-One) or Server Information panel (Lagom)
 * - lift our custom card out of nested theme chrome without hiding Lagom #Overview
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
    // When WHMCS exposes the module name, skip non-RackFlow products early.
    // Otherwise fall through and rely on #rackflow-client-area presence.
    if ($module !== '' && $module !== 'rackflow') {
        return '';
    }

    return <<<'HTML'
<style id="rackflow-client-area-hide-tabs">
  /* Only when our module card is on the page. */
  body:has(#rackflow-client-area) .nav-tabs a[href="#configoptions"],
  body:has(#rackflow-client-area) li.nav-item:has(> a[href="#configoptions"]),
  body:has(#rackflow-client-area) li:has(> a[href="#configoptions"]),
  body:has(#rackflow-client-area) #configoptions,
  body:has(#rackflow-client-area) .nav-tabs a[href="#additionalinfo"],
  body:has(#rackflow-client-area) li.nav-item:has(> a[href="#additionalinfo"]),
  body:has(#rackflow-client-area) li:has(> a[href="#additionalinfo"]),
  body:has(#rackflow-client-area) #additionalinfo,
  body:has(#rackflow-client-area) a[href="#tabChangepw"],
  body:has(#rackflow-client-area) li:has(> a[href="#tabChangepw"]),
  body:has(#rackflow-client-area) #tabChangepw,
  /* Lagom 2 uses #Changepw (no "tab" prefix) */
  body:has(#rackflow-client-area) a[href="#Changepw"],
  body:has(#rackflow-client-area) li:has(> a[href="#Changepw"]),
  body:has(#rackflow-client-area) #Changepw,
  body:has(#rackflow-client-area) a[href="#domain"],
  body:has(#rackflow-client-area) li:has(> a[href="#domain"]),
  body:has(#rackflow-client-area) .list-group-item[href="#domain"] {
    display: none !important;
  }
  body:has(#rackflow-client-area) #domain > .row,
  body:has(#rackflow-client-area) #manage > .row {
    display: none !important;
  }
  body:has(#rackflow-client-area) #domain .module-client-area,
  body:has(#rackflow-client-area) #manage .module-client-area {
    display: block !important;
    text-align: left;
  }
  /* After unwrap: card sits outside the theme tab box, full content width. */
  #rackflow-client-area-slot {
    margin: 0 0 18px;
    width: 100%;
  }
  #rackflow-client-area-slot .rf-ca,
  #rackflow-client-area-slot .rf-ca__card {
    width: 100%;
    max-width: none;
  }
  /* Lagom nests module output in an extra bordered panel — flatten it. */
  body.lagom #rackflow-client-area-slot,
  body.lagom .module-client-area.module-rackflow {
    width: 100%;
  }
  body.lagom.rf-ca-unwrapped .panel-product-details:not(:has(#rackflow-client-area)):not(:has(.module-client-area)) {
    display: none !important;
  }
  body.rf-ca-unwrapped .rf-ca-hide-tabs,
  body.rf-ca-unwrapped .rf-ca-hide-tabs-connector,
  body.rf-ca-unwrapped .rf-ca-hide-tabpane,
  body.rf-ca-unwrapped .rf-ca-hide-section {
    display: none !important;
  }
  body.rf-ca-unwrapped .rf-ca-plain-tabs.product-details-tab-container {
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

  function movePasswordModalToBody() {
    var pwModal = document.getElementById('rackflow-password-modal');
    if (pwModal && pwModal.parentNode !== document.body) {
      document.body.appendChild(pwModal);
    }
  }

  function ensureSlotBefore(anchor) {
    var slot = document.getElementById('rackflow-client-area-slot');
    if (!slot) {
      slot = document.createElement('div');
      slot.id = 'rackflow-client-area-slot';
    }
    if (anchor && anchor.parentNode) {
      if (slot.parentNode !== anchor.parentNode || slot.nextSibling !== anchor) {
        anchor.parentNode.insertBefore(slot, anchor);
      }
    }
    return slot;
  }

  /** Lagom 2: module card lives in #Overview inside .panel-product-details — do not hide Overview. */
  function unwrapLagom(card) {
    var lagomPanel = card.closest('.panel-product-details');
    var productDetails = document.querySelector('#Overview .product-details');
    var anchor = lagomPanel || card.closest('.module-client-area');
    if (!anchor) {
      return false;
    }

    // Prefer placing the card right after the product status block.
    var slot;
    if (productDetails && productDetails.parentNode) {
      slot = document.getElementById('rackflow-client-area-slot');
      if (!slot) {
        slot = document.createElement('div');
        slot.id = 'rackflow-client-area-slot';
      }
      if (productDetails.nextSibling !== slot) {
        productDetails.parentNode.insertBefore(slot, productDetails.nextSibling);
      }
    } else {
      slot = ensureSlotBefore(anchor);
    }
    slot.appendChild(card);

    if (lagomPanel && !lagomPanel.contains(card)) {
      lagomPanel.classList.add('rf-ca-hide-section');
      hideEl(lagomPanel);
    }

    // Hide Server Information / configurable-options details section (hostname, NS, …).
    var domainPane = document.getElementById('domain');
    if (domainPane) {
      var detailsSection = domainPane.closest('.section');
      if (detailsSection) {
        detailsSection.classList.add('rf-ca-hide-section');
        hideEl(detailsSection);
      } else {
        hideEl(domainPane);
      }
    }

    // Lagom Change Password is a top-level pane (#Changepw), not #tabChangepw.
    hideHrefTargets(['#Changepw', '#tabChangepw', '#configoptions', '#additionalinfo', '#domain']);
    hideEl(document.getElementById('Changepw'));

    // Keep Overview selected if the URL hash pointed at Change Password.
    var overview = document.getElementById('Overview');
    if (overview) {
      overview.classList.add('active');
      overview.style.display = '';
      overview.classList.remove('rf-ca-hide-tabpane');
    }

    card.dataset.rfUnwrapped = '1';
    document.body.classList.add('rf-ca-unwrapped');
    movePasswordModalToBody();
    return true;
  }

  /** Twenty-One / legacy: module output is nested inside #domain / #manage. */
  function unwrapLegacy(card) {
    hideHrefTargets(['#configoptions', '#additionalinfo', '#tabChangepw', '#Changepw']);

    var domainRows = document.querySelectorAll('#domain > .row, #manage > .row');
    for (var r = 0; r < domainRows.length; r++) {
      hideEl(domainRows[r]);
    }

    // Important: only #domain/#manage — never a bare .tab-pane (Lagom #Overview).
    var tabPane = card.closest('#domain, #manage');
    if (!tabPane) {
      movePasswordModalToBody();
      card.dataset.rfUnwrapped = '1';
      document.body.classList.add('rf-ca-unwrapped');
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

    card.dataset.rfUnwrapped = '1';
    document.body.classList.add('rf-ca-unwrapped');
    movePasswordModalToBody();

    if (tabList) {
      var serverTabs = tabList.querySelectorAll('a[href="#domain"], a[href="#manage"]');
      for (var t = 0; t < serverTabs.length; t++) {
        var li = serverTabs[t].closest('li') || serverTabs[t];
        li.classList.add('rf-ca-hide-tabs');
        hideEl(li);
      }
    }
    tabPane.classList.add('rf-ca-hide-tabpane');
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
        tabList.classList.add('rf-ca-hide-tabs');
        hideEl(tabList);
      }
      if (connector) {
        connector.classList.add('rf-ca-hide-tabs-connector');
        hideEl(connector);
      }
      if (tabContent) {
        var otherPanes = tabContent.querySelectorAll(':scope > .tab-pane');
        var visiblePanes = 0;
        for (var p = 0; p < otherPanes.length; p++) {
          if (otherPanes[p] === tabPane) continue;
          if (otherPanes[p].style.display === 'none') continue;
          if (window.getComputedStyle(otherPanes[p]).display === 'none') continue;
          var id = otherPanes[p].id ? ('#' + otherPanes[p].id) : '';
          if (id && tabList && tabList.querySelector('a[href="' + id + '"]')) {
            var tabLi = tabList.querySelector('a[href="' + id + '"]').closest('li');
            if (tabLi && tabLi.style.display !== 'none' && window.getComputedStyle(tabLi).display !== 'none') {
              visiblePanes++;
            }
          }
        }
        if (visiblePanes === 0) {
          tabContent.classList.add('rf-ca-hide-tabpane');
          hideEl(tabContent);
        } else {
          tabContent.classList.add('rf-ca-plain-tabs');
        }
      }
    } else if (tabContent) {
      tabContent.classList.add('rf-ca-plain-tabs');
    }
  }

  function unwrapRackflowClientArea() {
    var card = document.getElementById('rackflow-client-area');
    if (!card || card.dataset.rfUnwrapped === '1') {
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
    document.addEventListener('DOMContentLoaded', unwrapRackflowClientArea);
  } else {
    unwrapRackflowClientArea();
  }
})();
</script>
HTML;
});

add_hook('AdminAreaFooterOutput', 2, function (array $vars) {
    $filename = isset($vars['filename']) ? (string)$vars['filename'] : '';
    $uri = isset($_SERVER['REQUEST_URI']) ? (string)$_SERVER['REQUEST_URI'] : '';
    $onService = (
        $filename === 'clientsservices'
        || strpos($uri, 'clientsservices.php') !== false
    );
    if (!$onService) {
        return '';
    }

    return <<<'HTML'
<style id="rackflow-admin-status-css">
  .rf-as {
    --rf-ink: #1c2430;
    --rf-muted: #5b6775;
    --rf-line: #d5dce5;
    --rf-bg: #f4f6f8;
    --rf-card: #ffffff;
    --rf-accent: #0f6e56;
    --rf-accent-soft: #e7f5ef;
    --rf-danger: #b42318;
    --rf-danger-soft: #fdecea;
    --rf-radius: 12px;
    width: 100%;
    max-width: none;
    margin: 0;
    text-align: left;
    color: var(--rf-ink);
    font-family: "Segoe UI", system-ui, -apple-system, sans-serif;
  }
  .rf-as *, .rf-as *::before, .rf-as *::after { box-sizing: border-box; }
  .rf-as__card {
    background: var(--rf-card);
    border: 1px solid var(--rf-line);
    border-radius: var(--rf-radius);
    overflow: hidden;
  }
  .rf-as__head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    padding: 16px 18px;
    background: linear-gradient(135deg, #f7faf8 0%, #eef3f7 100%);
    border-bottom: 1px solid var(--rf-line);
  }
  .rf-as__head-right {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
    justify-content: flex-end;
  }
  .rf-as__head-actions { display: flex; gap: 8px; }
  .rf-as__title {
    margin: 0;
    font-size: 15px;
    font-weight: 700;
    letter-spacing: -0.02em;
  }
  .rf-as__subtitle {
    margin: 4px 0 0;
    font-size: 12px;
    color: var(--rf-muted);
    line-height: 1.4;
  }
  .rf-as__badge {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 5px 10px;
    border-radius: 999px;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    white-space: nowrap;
  }
  .rf-as__badge--on { background: #d8f3e3; color: #0b6b3a; }
  .rf-as__badge--off { background: #e8ecf0; color: #4a5560; }
  .rf-as__badge--warn { background: #fff3cd; color: #7a5b00; }
  .rf-as__badge--unknown { background: #e8ecf0; color: #4a5560; }
  .rf-as__badge-dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: currentColor;
  }
  .rf-as__body { padding: 16px 18px 18px; }
  .rf-as__grid {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 10px;
    margin-bottom: 14px;
  }
  .rf-as__stat {
    background: var(--rf-bg);
    border: 1px solid var(--rf-line);
    border-radius: 10px;
    padding: 11px 12px;
    min-height: 64px;
  }
  .rf-as__stat-label {
    display: block;
    margin: 0 0 5px;
    font-size: 10px;
    font-weight: 750;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    color: var(--rf-muted);
  }
  .rf-as__stat-value {
    margin: 0;
    font-size: 13px;
    font-weight: 650;
    word-break: break-word;
    line-height: 1.35;
  }
  .rf-as__actions { display: grid; gap: 10px; margin-bottom: 12px; }
  .rf-as__action {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 14px;
    padding: 12px 14px;
    border: 1px solid var(--rf-line);
    border-radius: 10px;
    background: var(--rf-bg);
  }
  .rf-as__action-copy { min-width: 0; flex: 1; }
  .rf-as__action-title {
    margin: 0 0 3px;
    font-size: 13px;
    font-weight: 700;
  }
  .rf-as__action-help {
    margin: 0;
    font-size: 11px;
    color: var(--rf-muted);
    line-height: 1.4;
  }
  .rf-as__creds {
    margin: 6px 0 0;
    font-size: 12px;
    color: var(--rf-muted);
  }
  .rf-as__creds code {
    color: var(--rf-ink);
    font-size: 12px;
  }
  .rf-as__btn,
  a.rf-as__btn,
  button.rf-as__btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    min-width: 110px;
    padding: 8px 14px;
    border-radius: 8px;
    border: 1px solid var(--rf-accent);
    background: var(--rf-accent);
    color: #fff !important;
    font-size: 12px;
    font-weight: 700;
    text-decoration: none !important;
    white-space: nowrap;
    cursor: pointer;
    transition: background 0.15s ease, border-color 0.15s ease;
  }
  .rf-as__btn:hover,
  .rf-as__btn:focus,
  a.rf-as__btn:hover,
  a.rf-as__btn:focus,
  button.rf-as__btn:hover {
    background: #0c5c48;
    border-color: #0c5c48;
    color: #fff !important;
  }
  .rf-as__btn:disabled {
    opacity: 0.55;
    cursor: not-allowed;
  }
  .rf-as__btn--secondary,
  a.rf-as__btn--secondary,
  button.rf-as__btn--secondary {
    background: #fff;
    color: var(--rf-ink) !important;
    border-color: var(--rf-line);
  }
  .rf-as__btn--secondary:hover,
  .rf-as__btn--secondary:focus,
  a.rf-as__btn--secondary:hover,
  button.rf-as__btn--secondary:hover {
    background: var(--rf-accent-soft);
    border-color: var(--rf-accent);
    color: var(--rf-ink) !important;
  }
  .rf-as__btn--danger,
  button.rf-as__btn--danger {
    background: #fff;
    color: var(--rf-danger) !important;
    border-color: #f0b4ae;
  }
  .rf-as__btn--danger:hover,
  button.rf-as__btn--danger:hover {
    background: var(--rf-danger-soft);
    border-color: var(--rf-danger);
    color: var(--rf-danger) !important;
  }
  .rf-as__btn--sm {
    min-width: 0;
    padding: 6px 10px;
    font-size: 11px;
  }
  .rf-as__note {
    margin: 0 0 14px;
    padding: 12px 14px;
    border: 1px solid var(--rf-line);
    border-radius: 10px;
    background: var(--rf-bg);
    font-size: 13px;
    color: var(--rf-muted);
    line-height: 1.45;
  }
  .rf-as__panel {
    margin-top: 4px;
    padding: 14px;
    border: 1px solid var(--rf-line);
    border-radius: 10px;
    background: var(--rf-bg);
  }
  .rf-as__panel-head {
    margin-bottom: 12px;
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 10px;
    flex-wrap: wrap;
  }
  .rf-as__panel-title {
    margin: 0 0 4px;
    font-size: 13px;
    font-weight: 700;
  }
  .rf-as__panel-help {
    margin: 0;
    font-size: 11px;
    color: var(--rf-muted);
    line-height: 1.4;
  }
  .rf-as__proxy-row {
    padding: 8px 0;
    border-bottom: 1px solid var(--rf-line);
  }
  .rf-as__proxy-row:last-of-type { border-bottom: none; }
  .rf-as__proxy-url {
    margin: 2px 0 0;
    font-size: 11px;
    color: var(--rf-muted);
    word-break: break-all;
  }
  .rf-as__proxy-url code { color: var(--rf-ink); }
  .rf-as__sr-only {
    position: absolute !important;
    width: 1px !important;
    height: 1px !important;
    padding: 0 !important;
    margin: -1px !important;
    overflow: hidden !important;
    clip: rect(0, 0, 0, 0) !important;
    white-space: nowrap !important;
    border: 0 !important;
  }
  #rf-as-proxy-rotate, #rf-as-proxy-copy-all { margin-top: 0; }
  #rf-as-proxy-rotate { margin-top: 8px; }
  .rf-as__backup-create {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin: 0 0 12px;
    align-items: center;
  }
  .rf-as__backup-create .rf-as__input,
  #rf-as-backup-notes {
    flex: 1 1 180px;
    min-width: 160px;
    padding: 8px 10px;
    border: 1px solid var(--rf-line);
    border-radius: 8px;
    font-size: 13px;
    background: #fff;
    color: var(--rf-ink);
  }
  .rf-as__backup-list {
    list-style: none;
    margin: 0 0 12px;
    padding: 0;
    display: grid;
    gap: 8px;
  }
  .rf-as__backup-item {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
    padding: 8px 10px;
    border-radius: 8px;
    font-size: 13px;
    border: 1px solid var(--rf-line);
    background: #f4f6f8;
  }
  .rf-as__backup-item--platform {
    background: #eef4fb;
    border-color: #c5d7eb;
  }
  .rf-as__backup-item--client {
    background: #eef8f3;
    border-color: #b9dcc9;
  }
  .rf-as__backup-item--running {
    background: #fff8e6;
    border-color: #f0d78c;
  }
  .rf-as__backup-kind {
    display: inline-flex;
    align-items: center;
    padding: 2px 8px;
    border-radius: 999px;
    font-size: 11px;
    font-weight: 750;
    letter-spacing: 0.02em;
    text-transform: none;
    line-height: 1.4;
  }
  .rf-as__backup-kind--platform {
    background: #d9e7f7;
    color: #1f4b7a;
  }
  .rf-as__backup-kind--client {
    background: #d5ebe0;
    color: #1f5c40;
  }
  .rf-as__backup-kind--restore,
  .rf-as__backup-kind--backup {
    background: #f0e6c8;
    color: #6a5214;
  }
  .rf-as__backup-template {
    display: inline-flex;
    align-items: center;
    padding: 2px 8px;
    border-radius: 6px;
    background: rgba(28, 36, 48, 0.06);
    color: var(--rf-muted);
    font-size: 12px;
    font-weight: 600;
  }
  .rf-as__backup-title {
    font-weight: 700;
    color: var(--rf-ink);
  }
  .rf-as__backup-storage,
  .rf-as__backup-when {
    color: var(--rf-muted);
  }
  .rf-as__backup-running-label {
    font-weight: 700;
    text-transform: uppercase;
    font-size: 11px;
    letter-spacing: 0.04em;
    color: #6a5214;
  }
  .rf-as__ip-current-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 4px;
  }
  .rf-as__ip-current {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 8px 10px;
    min-width: 0;
  }
  .rf-as__ip-current .rf-as__stat-label {
    margin: 0;
    flex: 0 0 auto;
  }
  .rf-as__ip-current .rf-as__stat-value {
    font-size: 15px;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
  }
  .rf-as__reinstall-form { display: grid; gap: 10px; }
  .rf-as__reinstall-row {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: flex-end;
  }
  .rf-as__reinstall-field { flex: 1 1 220px; min-width: 180px; }
  .rf-as__reinstall-field .rf-as__input { width: 100%; }
  .rf-as__textarea {
    min-height: 72px;
    resize: vertical;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 12px;
    line-height: 1.4;
  }
  .rf-as__link-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 12px;
  }
  .rf-as__search { display: grid; gap: 10px; }
  .rf-as__seg {
    display: inline-flex;
    gap: 6px;
    flex-wrap: wrap;
  }
  .rf-as__seg-opt {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    margin: 0;
    padding: 6px 10px;
    border: 1px solid var(--rf-line);
    border-radius: 999px;
    background: #fff;
    font-size: 12px;
    font-weight: 650;
    cursor: pointer;
  }
  .rf-as__seg-opt:has(input:checked) {
    border-color: var(--rf-accent);
    background: var(--rf-accent-soft);
  }
  .rf-as__seg-opt input { margin: 0; }
  .rf-as__search-row {
    display: flex;
    gap: 8px;
    align-items: center;
  }
  .rf-as__input {
    flex: 1;
    min-width: 0;
    height: 36px;
    padding: 0 12px;
    border: 1px solid var(--rf-line);
    border-radius: 8px;
    background: #fff;
    color: var(--rf-ink);
    font-size: 13px;
  }
  .rf-as__input:focus {
    outline: none;
    border-color: var(--rf-accent);
    box-shadow: 0 0 0 2px var(--rf-accent-soft);
  }
  .rf-as__results {
    display: grid;
    gap: 8px;
  }
  .rf-as__result {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 10px 12px;
    border: 1px solid var(--rf-line);
    border-radius: 8px;
    background: #fff;
  }
  .rf-as__result-meta {
    margin: 0;
    font-size: 12px;
    color: var(--rf-muted);
    line-height: 1.4;
  }
  .rf-as__result-title {
    margin: 0 0 2px;
    font-size: 13px;
    font-weight: 700;
  }
  .rf-as__msg {
    margin: 0;
    font-size: 12px;
    color: var(--rf-muted);
  }
  .rf-as__msg.is-error { color: var(--rf-danger); }
  .rf-as__msg.is-ok { color: #0b6b3a; }
  .rf-as__vmid {
    margin-top: 12px;
    padding-top: 12px;
    border-top: 1px solid var(--rf-line);
  }
  .rf-as__foot {
    margin: 12px 0 0;
    font-size: 11px;
    color: var(--rf-muted);
  }
  /*
   * Keep the card in the form's fieldarea column so its left edge lines up
   * with Module Commands / inputs (not flush under the label column).
   * Clear the label text but keep the cell so column geometry stays intact.
   */
  tr.rf-as-row > td.fieldlabel,
  tr.rf-as-row > th.fieldlabel {
    font-size: 0 !important;
    line-height: 0 !important;
    color: transparent !important;
    visibility: hidden !important;
  }
  tr.rf-as-row > td.fieldarea,
  tr.rf-as-row > td:last-child {
    width: auto !important;
    padding-top: 12px !important;
    padding-bottom: 12px !important;
  }
  tr.rf-as-row .rf-as,
  tr.rf-as-row .rf-as__card {
    width: 100%;
    max-width: none;
    margin: 0;
  }
  tr.rf-as-cf-hidden { display: none !important; }
  @media (max-width: 900px) {
    .rf-as__grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  }
  @media (max-width: 720px) {
    .rf-as__grid { grid-template-columns: 1fr; }
    .rf-as__head { flex-direction: column; align-items: flex-start; }
    .rf-as__head-right { justify-content: flex-start; }
    .rf-as__action,
    .rf-as__link-row,
    .rf-as__result { flex-direction: column; align-items: stretch; }
    .rf-as__btn { width: 100%; min-width: 0; }
    .rf-as__search-row { flex-direction: column; align-items: stretch; }
  }
</style>
<script>
(function () {
  function enhanceRackflowAdminService() {
    var card = document.getElementById('rackflow-admin-status');
    if (!card || card.dataset.rfBound === '1') { return; }
    card.dataset.rfBound = '1';

    var statusRow = card.closest('tr');
    if (statusRow) {
      statusRow.classList.add('rf-as-row');
      var statusLabel = statusRow.querySelector('td.fieldlabel, th.fieldlabel');
      if (statusLabel) {
        statusLabel.textContent = '';
        statusLabel.setAttribute('aria-hidden', 'true');
      }
    }

    var rows = document.querySelectorAll('tr');
    for (var i = 0; i < rows.length; i++) {
      var row = rows[i];
      var label = row.querySelector('td.fieldlabel, th.fieldlabel, td:first-child');
      if (!label) { continue; }
      var text = (label.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase();
      if (text === 'rackflow service id') {
        row.classList.add('rf-as-cf-hidden');
      }
    }

    var endpoint = card.getAttribute('data-admin-link-url') || '';
    var serviceId = parseInt(card.getAttribute('data-serviceid') || '0', 10);
    var msgEl = document.getElementById('rf-as-msg');
    var resultsEl = document.getElementById('rf-as-results');
    var unlinkBtn = document.getElementById('rf-as-unlink');
    var searchBtn = document.getElementById('rf-as-search-btn');
    var searchQ = document.getElementById('rf-as-search-q');
    var openBtn = document.getElementById('rf-as-open');
    var linkLabel = document.getElementById('rf-as-link-label');
    var currentLink = document.getElementById('rf-as-current-link');
    var vmidPanel = document.getElementById('rf-as-vmid-panel');
    var vmidInput = document.getElementById('rf-as-vmid-input');
    var vmidSave = document.getElementById('rf-as-vmid-save');

    function setMsg(text, kind) {
      if (!msgEl) { return; }
      if (!text) {
        msgEl.hidden = true;
        msgEl.textContent = '';
        msgEl.className = 'rf-as__msg';
        return;
      }
      msgEl.hidden = false;
      msgEl.textContent = text;
      msgEl.className = 'rf-as__msg' + (kind ? ' is-' + kind : '');
    }

    function setBusy(btn, busy) {
      if (!btn) { return; }
      btn.disabled = !!busy;
    }

    function updateLinkUi(rfId, openUrl, service) {
      var linked = !!rfId;
      card.setAttribute('data-rackflow-id', linked ? String(rfId) : '');
      if (openUrl) {
        card.setAttribute('data-open-url', openUrl);
      }
      var label = linked ? ('RackFlow #' + rfId) : 'Not linked';
      if (linkLabel) { linkLabel.textContent = label; }
      if (currentLink) { currentLink.textContent = label; }
      if (unlinkBtn) { unlinkBtn.hidden = !linked; }
      if (openBtn) {
        if (linked && openUrl) {
          openBtn.hidden = false;
          openBtn.href = openUrl;
        } else if (linked && card.getAttribute('data-open-url')) {
          openBtn.hidden = false;
          openBtn.href = card.getAttribute('data-open-url');
        } else {
          openBtn.hidden = true;
        }
      }
      if (service) {
        if (service.proxmox_cluster_id != null) {
          card.setAttribute('data-cluster-id', String(service.proxmox_cluster_id));
        }
        if (service.proxmox_node_name) {
          card.setAttribute('data-node-name', String(service.proxmox_node_name));
        }
        if (service.proxmox_vmid != null) {
          card.setAttribute('data-vmid', String(service.proxmox_vmid));
          if (vmidInput) { vmidInput.value = String(service.proxmox_vmid); }
        }
      }
      if (vmidPanel) {
        var st = card.getAttribute('data-service-type') || '';
        var cid = card.getAttribute('data-cluster-id') || '';
        var node = card.getAttribute('data-node-name') || '';
        vmidPanel.hidden = !(linked && st === 'vm' && cid && node);
      }
    }

    function postAction(payload, btn) {
      if (!endpoint || !serviceId) {
        setMsg('Admin link endpoint is not available.', 'error');
        return Promise.reject();
      }
      setBusy(btn, true);
      setMsg('Working…');
      return fetch(endpoint, {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
          'Content-Type': 'application/json',
          'Accept': 'application/json',
          'X-Requested-With': 'XMLHttpRequest'
        },
        body: JSON.stringify(Object.assign({ serviceid: serviceId }, payload || {}))
      }).then(function (res) {
        return res.json().catch(function () {
          return { ok: false, error: 'Invalid response from server (' + res.status + ').' };
        }).then(function (data) {
          return { res: res, data: data };
        });
      }).then(function (out) {
        setBusy(btn, false);
        if (!out.data || !out.data.ok) {
          setMsg((out.data && out.data.error) ? out.data.error : 'Request failed.', 'error');
          return Promise.reject(out.data);
        }
        return out.data;
      }).catch(function (err) {
        setBusy(btn, false);
        if (err && err.error) { return Promise.reject(err); }
        setMsg('Network error talking to WHMCS.', 'error');
        return Promise.reject(err);
      });
    }

    function renderResults(rows) {
      if (!resultsEl) { return; }
      resultsEl.innerHTML = '';
      if (!rows || !rows.length) {
        resultsEl.hidden = false;
        resultsEl.innerHTML = '<p class="rf-as__msg">No matching services or Proxmox VMs.</p>';
        return;
      }
      resultsEl.hidden = false;
      rows.forEach(function (row) {
        var isProxmox = (row.source === 'proxmox') || !row.id;
        var item = document.createElement('div');
        item.className = 'rf-as__result';
        var copy = document.createElement('div');
        var title = document.createElement('p');
        title.className = 'rf-as__result-title';
        if (isProxmox) {
          title.textContent = (row.name || 'Proxmox VM') + ' · unmanaged';
        } else {
          title.textContent = '#' + row.id + (row.name ? (' · ' + row.name) : '');
        }
        var meta = document.createElement('p');
        meta.className = 'rf-as__result-meta';
        var bits = [];
        if (isProxmox) { bits.push('proxmox'); }
        else if (row.service_type) { bits.push(row.service_type); }
        if (row.status) { bits.push(row.status); }
        if (row.proxmox_status && isProxmox) { bits.push(row.proxmox_status); }
        if (row.proxmox_vmid != null) { bits.push('VMID ' + row.proxmox_vmid); }
        if (row.proxmox_node_name) { bits.push(row.proxmox_node_name); }
        if (row.server_ip) { bits.push(row.server_ip); }
        else if (row.server_name) { bits.push(row.server_name); }
        if (!isProxmox) {
          if (row.external_service_id) { bits.push('ext ' + row.external_service_id); }
          else { bits.push('unlinked'); }
        }
        meta.textContent = bits.join(' · ');
        copy.appendChild(title);
        copy.appendChild(meta);
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'rf-as__btn rf-as__btn--sm';
        btn.textContent = isProxmox ? 'Adopt & link' : 'Link';
        btn.addEventListener('click', function () {
          if (isProxmox) {
            if (!window.confirm(
              'Adopt Proxmox VMID ' + row.proxmox_vmid + ' (' + (row.name || 'unnamed') +
              ') into RackFlow and link WHMCS service ' + serviceId + '?'
            )) {
              return;
            }
            postAction({
              action: 'adopt',
              proxmox_cluster_id: row.proxmox_cluster_id,
              proxmox_node_name: row.proxmox_node_name,
              proxmox_vmid: row.proxmox_vmid,
              name: row.name || null
            }, btn).then(function (data) {
              setMsg('Adopted and linked as RackFlow #' + data.rackflow_service_id + '. Reload for live status.', 'ok');
              updateLinkUi(data.rackflow_service_id, data.open_url, data.service);
              resultsEl.hidden = true;
              resultsEl.innerHTML = '';
            });
            return;
          }
          if (!window.confirm('Link WHMCS service ' + serviceId + ' to RackFlow #' + row.id + '?')) {
            return;
          }
          postAction({ action: 'link', rackflow_service_id: row.id }, btn).then(function (data) {
            setMsg('Linked to RackFlow #' + data.rackflow_service_id + '. Reload for live status.', 'ok');
            updateLinkUi(data.rackflow_service_id, data.open_url, data.service);
            resultsEl.hidden = true;
            resultsEl.innerHTML = '';
          });
        });
        item.appendChild(copy);
        item.appendChild(btn);
        resultsEl.appendChild(item);
      });
    }

    if (searchBtn) {
      searchBtn.addEventListener('click', function () {
        var q = (searchQ && searchQ.value || '').trim();
        var qType = 'any';
        var radios = card.querySelectorAll('input[name="rf_as_qtype"]');
        for (var r = 0; r < radios.length; r++) {
          if (radios[r].checked) { qType = radios[r].value; break; }
        }
        postAction({ action: 'search', q_type: qType, q: q }, searchBtn).then(function (data) {
          setMsg('', '');
          renderResults(data.results || []);
        });
      });
    }
    if (searchQ) {
      searchQ.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') {
          e.preventDefault();
          if (searchBtn) { searchBtn.click(); }
        }
      });
    }
    if (unlinkBtn) {
      unlinkBtn.addEventListener('click', function () {
        var rfId = card.getAttribute('data-rackflow-id') || '';
        if (!window.confirm('Unlink this WHMCS service from RackFlow' + (rfId ? (' #' + rfId) : '') + '?')) {
          return;
        }
        postAction({ action: 'unlink', rackflow_service_id: rfId ? parseInt(rfId, 10) : null }, unlinkBtn).then(function () {
          setMsg('Unlinked from RackFlow.', 'ok');
          updateLinkUi(null, '', null);
          card.setAttribute('data-open-url', '');
          if (resultsEl) { resultsEl.hidden = true; resultsEl.innerHTML = ''; }
        });
      });
    }
    if (vmidSave) {
      vmidSave.addEventListener('click', function () {
        var rfId = card.getAttribute('data-rackflow-id') || '';
        var clusterId = card.getAttribute('data-cluster-id') || '';
        var nodeName = card.getAttribute('data-node-name') || '';
        var vmid = (vmidInput && vmidInput.value || '').trim();
        postAction({
          action: 'set_vmid',
          rackflow_service_id: rfId ? parseInt(rfId, 10) : null,
          proxmox_cluster_id: parseInt(clusterId, 10),
          proxmox_node_name: nodeName,
          proxmox_vmid: parseInt(vmid, 10)
        }, vmidSave).then(function (data) {
          setMsg('VMID updated.', 'ok');
          updateLinkUi(rfId ? parseInt(rfId, 10) : null, card.getAttribute('data-open-url'), data.service || null);
        });
      });
    }

    // Proxy credential copy-all + rotation via AJAX.
    var proxyCopyBtn = document.getElementById('rf-as-proxy-copy-all');
    if (proxyCopyBtn) {
      proxyCopyBtn.addEventListener('click', function () {
        var linesEl = document.getElementById('rf-as-proxy-endpoint-lines');
        var text = linesEl ? linesEl.value : '';
        if (!text) { return; }
        var original = proxyCopyBtn.textContent;
        function markDone() {
          proxyCopyBtn.textContent = 'Copied';
          window.setTimeout(function () { proxyCopyBtn.textContent = original; }, 2000);
        }
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(markDone).catch(function () {
            linesEl.focus();
            linesEl.select();
            try { document.execCommand('copy'); } catch (e) {}
            markDone();
          });
        } else {
          linesEl.focus();
          linesEl.select();
          try { document.execCommand('copy'); } catch (e) {}
          markDone();
        }
      });
    }
    var proxyRotateBtn = document.getElementById('rf-as-proxy-rotate');
    var proxyMsgEl = document.getElementById('rf-as-proxy-msg');
    if (proxyRotateBtn) {
      proxyRotateBtn.addEventListener('click', function () {
        if (!window.confirm('Rotate proxy credentials? The old username/password stop working immediately.')) {
          return;
        }
        var rfId = card.getAttribute('data-rackflow-id') || '';
        postAction({ action: 'rotate_proxy', rackflow_service_id: rfId ? parseInt(rfId, 10) : null }, proxyRotateBtn).then(function () {
          setMsg('Credentials rotated. Reload to see the new values.', 'ok');
          if (proxyMsgEl) {
            proxyMsgEl.hidden = false;
            proxyMsgEl.textContent = 'Rotated — reload the page to see the new credentials.';
          }
        });
      });
    }

    // VM IP browse / reassign via AJAX.
    var ipPanel = document.getElementById('rf-as-ip-panel');
    var ipRefreshBtn = document.getElementById('rf-as-ip-refresh');
    var ipApplyBtn = document.getElementById('rf-as-ip-apply');
    var ipSelect = document.getElementById('rf-as-ip-select');
    var ipForm = document.getElementById('rf-as-ip-form');
    var ipReset = document.getElementById('rf-as-ip-reset');
    var ipCurrentEl = document.getElementById('rf-as-ip-current');
    var ipMsgEl = document.getElementById('rf-as-ip-msg');
    function setIpMsg(text, isError) {
      if (!ipMsgEl) { return; }
      if (!text) {
        ipMsgEl.hidden = true;
        ipMsgEl.textContent = '';
        return;
      }
      ipMsgEl.hidden = false;
      ipMsgEl.textContent = text;
      ipMsgEl.style.color = isError ? '#b42318' : '#0a7a32';
    }
    function fillIpSelect(rows) {
      if (!ipSelect) { return; }
      ipSelect.innerHTML = '';
      (rows || []).forEach(function (row) {
        if (!row || row.id == null) { return; }
        var opt = document.createElement('option');
        opt.value = String(row.id);
        var label = row.ip_address || ('#' + row.id);
        if (row.gateway) { label += ' · gw ' + row.gateway; }
        if (row.bridge_name) { label += ' · ' + row.bridge_name; }
        if (row.batch_tag) { label += ' · ' + row.batch_tag; }
        opt.textContent = label;
        ipSelect.appendChild(opt);
      });
    }
    function runIpOp(op, extra, btn) {
      if (!ipPanel) { return Promise.reject(new Error('IP panel missing')); }
      var url = ipPanel.getAttribute('data-rf-ip-action') || '';
      var sid = ipPanel.getAttribute('data-rf-service-id') || '';
      if (!url || !sid) {
        setIpMsg('IP action URL missing. Reload the page.', true);
        return Promise.reject(new Error('missing url'));
      }
      var body = new URLSearchParams();
      body.set('serviceid', sid);
      body.set('op', op);
      if (extra) {
        Object.keys(extra).forEach(function (k) {
          if (extra[k] != null && extra[k] !== '') { body.set(k, extra[k]); }
        });
      }
      if (btn) { btn.disabled = true; }
      setIpMsg('Working…', false);
      return fetch(url, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
          'X-Requested-With': 'XMLHttpRequest'
        },
        body: body.toString(),
        credentials: 'same-origin'
      }).then(function (res) {
        return res.json().catch(function () {
          return { ok: false, error: 'Unexpected response (' + res.status + ')' };
        });
      }).then(function (data) {
        if (btn) { btn.disabled = false; }
        return data;
      }).catch(function (err) {
        if (btn) { btn.disabled = false; }
        throw err;
      });
    }
    if (ipRefreshBtn && ipPanel) {
      ipRefreshBtn.addEventListener('click', function () {
        runIpOp('list', null, ipRefreshBtn).then(function (data) {
          if (!data || !data.ok) {
            setIpMsg((data && data.error) ? data.error : 'Failed to list IPs.', true);
            return;
          }
          if (data.current && data.current.ip_address && ipCurrentEl) {
            ipCurrentEl.textContent = data.current.ip_address;
          }
          var rows = data.available || [];
          if (!rows.length) {
            if (ipForm) { ipForm.hidden = true; }
            setIpMsg('No free IPs for this cluster. Add pool rows in RackFlow VM IP allocations.', true);
            return;
          }
          fillIpSelect(rows);
          if (ipForm) { ipForm.hidden = false; }
          setIpMsg(rows.length + ' free IP' + (rows.length === 1 ? '' : 's') + ' available.', false);
        }).catch(function (err) {
          setIpMsg(err && err.message ? err.message : 'Failed to list IPs.', true);
        });
      });
    }
    if (ipApplyBtn && ipPanel) {
      ipApplyBtn.addEventListener('click', function () {
        if (!ipSelect || !ipSelect.value) {
          setIpMsg('Select a free IP first.', true);
          return;
        }
        var msg = ipApplyBtn.getAttribute('data-rf-confirm');
        if (msg && !window.confirm(msg)) { return; }
        var reset = !(ipReset && !ipReset.checked);
        runIpOp('reassign', {
          allocation_id: ipSelect.value,
          reset_network: reset ? '1' : '0'
        }, ipApplyBtn).then(function (data) {
          if (!data || !data.ok) {
            setIpMsg((data && data.error) ? data.error : 'IP reassignment failed.', true);
            return;
          }
          setIpMsg(data.message || 'IP reassigned.', false);
          if (data.data && data.data.vm_ip_address && ipCurrentEl) {
            ipCurrentEl.textContent = data.data.vm_ip_address;
          }
          window.setTimeout(function () { window.location.reload(); }, 900);
        }).catch(function (err) {
          setIpMsg(err && err.message ? err.message : 'IP reassignment failed.', true);
        });
      });
    }

    // VM reinstall via AJAX (template picker on the status card).
    var reinstallPanel = document.getElementById('rf-as-reinstall-panel');
    var reinstallBtn = document.getElementById('rf-as-reinstall-btn');
    var reinstallTemplate = document.getElementById('rf-as-reinstall-template');
    var reinstallMsgEl = document.getElementById('rf-as-reinstall-msg');
    function setReinstallMsg(text, isError) {
      if (!reinstallMsgEl) { return; }
      if (!text) {
        reinstallMsgEl.hidden = true;
        reinstallMsgEl.textContent = '';
        return;
      }
      reinstallMsgEl.hidden = false;
      reinstallMsgEl.textContent = text;
      reinstallMsgEl.style.color = isError ? '#b42318' : '#0a7a32';
    }
    var reinstallSsh = document.getElementById('rf-as-reinstall-ssh');
    var reinstallSshWrap = document.getElementById('rf-as-reinstall-ssh-wrap');
    function syncReinstallSshVisibility() {
      if (!reinstallTemplate || !reinstallSshWrap) { return; }
      var opt = reinstallTemplate.options[reinstallTemplate.selectedIndex];
      var accepts = !opt || !opt.value || opt.getAttribute('data-accepts-ssh') === '1';
      reinstallSshWrap.hidden = !accepts;
    }
    if (reinstallTemplate) {
      reinstallTemplate.addEventListener('change', syncReinstallSshVisibility);
      syncReinstallSshVisibility();
    }
    if (reinstallBtn && reinstallPanel) {
      reinstallBtn.addEventListener('click', function () {
        var msg = reinstallBtn.getAttribute('data-rf-confirm');
        if (msg && !window.confirm(msg)) { return; }
        var url = reinstallPanel.getAttribute('data-rf-reinstall-action') || '';
        var sid = reinstallPanel.getAttribute('data-rf-service-id') || '';
        if (!url || !sid) {
          setReinstallMsg('Reinstall action URL missing. Reload the page.', true);
          return;
        }
        var body = new URLSearchParams();
        body.set('serviceid', sid);
        if (reinstallTemplate && reinstallTemplate.value) {
          body.set('rf_vm_template_id', reinstallTemplate.value);
        }
        if (reinstallSsh && reinstallSshWrap && !reinstallSshWrap.hidden && reinstallSsh.value.trim()) {
          body.set('rf_ssh_public_keys', reinstallSsh.value);
        }
        reinstallBtn.disabled = true;
        setReinstallMsg('Working…', false);
        fetch(url, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'X-Requested-With': 'XMLHttpRequest'
          },
          body: body.toString(),
          credentials: 'same-origin'
        }).then(function (res) {
          return res.json().catch(function () {
            return { ok: false, error: 'Unexpected response (' + res.status + ')' };
          });
        }).then(function (data) {
          if (data && data.ok) {
            setReinstallMsg(data.message || 'Reinstall queued.', false);
            window.setTimeout(function () { window.location.reload(); }, 900);
            return;
          }
          setReinstallMsg((data && data.error) ? data.error : 'Reinstall failed.', true);
          reinstallBtn.disabled = false;
        }).catch(function (err) {
          setReinstallMsg(err && err.message ? err.message : 'Reinstall failed.', true);
          reinstallBtn.disabled = false;
        });
      });
    }

    var vmPanel = document.getElementById('rf-as-virtual-media-panel');
    if (vmPanel) {
      var vmMountBtn = document.getElementById('rf-as-virtual-media-mount');
      var vmEjectBtn = document.getElementById('rf-as-virtual-media-eject');
      var vmIso = document.getElementById('rf-as-virtual-media-iso');
      var vmBoot = document.getElementById('rf-as-virtual-media-boot-once');
      var vmMsg = document.getElementById('rf-as-virtual-media-msg');
      var vmStatus = document.getElementById('rf-as-virtual-media-status');
      function setVmMsg(text, isError) {
        if (!vmMsg) { return; }
        if (!text) {
          vmMsg.hidden = true;
          vmMsg.textContent = '';
          return;
        }
        vmMsg.hidden = false;
        vmMsg.textContent = text;
        vmMsg.style.color = isError ? '#b42318' : '#0a7a32';
      }
      function runAdminVirtualMedia(op, btn) {
        var url = vmPanel.getAttribute('data-rf-virtual-media-action') || '';
        var sid = vmPanel.getAttribute('data-rf-service-id') || '';
        if (!url || !sid) {
          setVmMsg('Virtual media URL missing. Reload the page.', true);
          return;
        }
        if (op === 'insert' && (!vmIso || !vmIso.value)) {
          setVmMsg('Select an ISO.', true);
          return;
        }
        var body = new URLSearchParams();
        body.set('serviceid', sid);
        body.set('op', op);
        if (vmIso && vmIso.value) { body.set('filename', vmIso.value); }
        if (vmBoot && vmBoot.checked) { body.set('boot_once', '1'); }
        if (btn) { btn.disabled = true; }
        setVmMsg('Working…', false);
        fetch(url, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'X-Requested-With': 'XMLHttpRequest'
          },
          body: body.toString(),
          credentials: 'same-origin'
        }).then(function (res) {
          return res.json().catch(function () {
            return { ok: false, error: 'Unexpected response (' + res.status + ')' };
          });
        }).then(function (data) {
          if (btn) { btn.disabled = false; }
          if (!data || !data.ok) {
            setVmMsg((data && data.error) ? data.error : 'Request failed.', true);
            return;
          }
          setVmMsg(data.message || 'Done.', false);
          if (vmStatus && data.data) {
            vmStatus.textContent = data.data.inserted
              ? ('Mounted: ' + (data.data.image_name || 'ISO'))
              : 'No virtual CD inserted';
          }
        }).catch(function (err) {
          if (btn) { btn.disabled = false; }
          setVmMsg(err && err.message ? err.message : 'Request failed.', true);
        });
      }
      if (vmMountBtn) {
        vmMountBtn.addEventListener('click', function () { runAdminVirtualMedia('insert', vmMountBtn); });
      }
      if (vmEjectBtn) {
        vmEjectBtn.addEventListener('click', function () { runAdminVirtualMedia('eject', vmEjectBtn); });
      }
    }

    // VM backups: create/delete/restore via AJAX (supports naming; no Module Commands).
    var backupsPanel = document.getElementById('rf-as-backups-panel');
    var backupCreateBtn = document.getElementById('rf-as-backup-create');
    var backupNotesInput = document.getElementById('rf-as-backup-notes');
    var backupMsgEl = document.getElementById('rf-as-backup-msg');
    function setBackupMsg(text, isError) {
      if (!backupMsgEl) { return; }
      if (!text) {
        backupMsgEl.hidden = true;
        backupMsgEl.textContent = '';
        return;
      }
      backupMsgEl.hidden = false;
      backupMsgEl.textContent = text;
      backupMsgEl.style.color = isError ? '#b42318' : '#0a7a32';
    }
    function runAdminBackupOp(op, extra, btn) {
      if (!backupsPanel) { return; }
      var url = backupsPanel.getAttribute('data-rf-backup-action') || '';
      var sid = backupsPanel.getAttribute('data-rf-service-id') || '';
      if (!url || !sid) {
        setBackupMsg('Backup action URL missing. Reload the page.', true);
        return;
      }
      var body = new URLSearchParams();
      body.set('serviceid', sid);
      body.set('op', op);
      if (extra) {
        Object.keys(extra).forEach(function (k) {
          if (extra[k] != null && extra[k] !== '') { body.set(k, extra[k]); }
        });
      }
      if (btn) { btn.disabled = true; }
      setBackupMsg('Working…', false);
      fetch(url, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
          'X-Requested-With': 'XMLHttpRequest'
        },
        body: body.toString(),
        credentials: 'same-origin'
      }).then(function (res) {
        return res.json().catch(function () {
          return { ok: false, error: 'Unexpected response (' + res.status + ')' };
        });
      }).then(function (data) {
        if (data && data.ok) {
          setBackupMsg(data.message || 'Done.', false);
          window.setTimeout(function () { window.location.reload(); }, 700);
          return;
        }
        setBackupMsg((data && data.error) ? data.error : 'Backup request failed.', true);
        if (btn) { btn.disabled = false; }
      }).catch(function (err) {
        setBackupMsg(err && err.message ? err.message : 'Backup request failed.', true);
        if (btn) { btn.disabled = false; }
      });
    }
    if (backupCreateBtn) {
      backupCreateBtn.addEventListener('click', function () {
        runAdminBackupOp('create', {
          rf_notes: backupNotesInput ? backupNotesInput.value : ''
        }, backupCreateBtn);
      });
    }
    if (backupsPanel) {
      var backupOpBtns = backupsPanel.querySelectorAll('[data-rf-backup-op]');
      for (var bi = 0; bi < backupOpBtns.length; bi++) {
        backupOpBtns[bi].addEventListener('click', function () {
          var msg = this.getAttribute('data-rf-confirm');
          if (msg && !window.confirm(msg)) { return; }
          runAdminBackupOp(this.getAttribute('data-rf-backup-op'), {
            volid: this.getAttribute('data-rf-volid') || '',
            storage: this.getAttribute('data-rf-storage') || ''
          }, this);
        });
      }
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', enhanceRackflowAdminService);
  } else {
    enhanceRackflowAdminService();
  }
})();
</script>
HTML;
});
