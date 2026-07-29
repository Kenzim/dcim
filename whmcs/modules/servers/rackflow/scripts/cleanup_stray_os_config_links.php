<?php
/**
 * One-off cleanup: unlink stray OS config groups from all RackFlow products.
 *
 * Ensures each rackflow product only keeps the canonical
 * "RackFlow Operating System" group link (plus any non-OS groups).
 *
 * Run from the WHMCS document root (CT 165):
 *
 *   cd /var/www && php modules/servers/rackflow/scripts/cleanup_stray_os_config_links.php
 *
 * Or via the host mount:
 *
 *   pct exec 165 -- php /var/www/modules/servers/rackflow/scripts/cleanup_stray_os_config_links.php
 */

if (PHP_SAPI !== 'cli') {
    fwrite(STDERR, "CLI only\n");
    exit(1);
}

$whmcsRoot = realpath(__DIR__ . '/../../../../..');
if ($whmcsRoot === false || !is_file($whmcsRoot . '/init.php')) {
    // When deployed under /var/www/modules/servers/rackflow/scripts
    $whmcsRoot = realpath(__DIR__ . '/../../../../');
}
if ($whmcsRoot === false || !is_file($whmcsRoot . '/init.php')) {
    fwrite(STDERR, "Could not locate WHMCS init.php from " . __DIR__ . "\n");
    exit(1);
}

require_once $whmcsRoot . '/init.php';
require_once dirname(__DIR__) . '/rackflow.php';

if (!class_exists('\Illuminate\Database\Capsule\Manager')) {
    fwrite(STDERR, "Capsule unavailable\n");
    exit(1);
}

$capsule = '\Illuminate\Database\Capsule\Manager';
$canonical = $capsule::table('tblproductconfiggroups')
    ->where('name', 'RackFlow Operating System')
    ->first();
if (!$canonical) {
    fwrite(STDOUT, "Canonical group not found; nothing to clean.\n");
    exit(0);
}
$canonicalId = (int)$canonical->id;

$products = $capsule::table('tblproducts')
    ->where('servertype', 'rackflow')
    ->orderBy('id')
    ->get();

$totalRemoved = 0;
foreach ($products as $product) {
    $pid = (int)$product->id;
    // Ensure the canonical link exists when customer OS selection is on, or
    // always ensure it for cleanup consistency; sync will hide options if needed.
    $link = $capsule::table('tblproductconfiglinks')
        ->where('gid', $canonicalId)
        ->where('pid', $pid)
        ->first();
    if (!$link) {
        $capsule::table('tblproductconfiglinks')->insert(array(
            'gid' => $canonicalId,
            'pid' => $pid,
        ));
        fwrite(STDOUT, "pid={$pid}: linked canonical OS group\n");
    }
    $removed = rackflow_unlinkStrayOsConfigGroups($pid, $canonicalId);
    if ($removed > 0) {
        fwrite(STDOUT, "pid={$pid}: removed {$removed} stray OS group link(s)\n");
        $totalRemoved += $removed;
    }
}

fwrite(STDOUT, "Done. Removed {$totalRemoved} stray link(s) across " . count($products) . " product(s).\n");
exit(0);
