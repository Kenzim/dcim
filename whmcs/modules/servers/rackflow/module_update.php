<?php
/**
 * Admin UI: pull RackFlow module files from git.
 *
 * Requires an authenticated WHMCS admin session. Uses WHMCS admin chrome
 * when available so the page sits in the normal admin layout.
 */

@ini_set('display_errors', '0');

require_once __DIR__ . '/../../../init.php';
if (is_file(__DIR__ . '/../../../includes/adminfunctions.php')) {
    require_once __DIR__ . '/../../../includes/adminfunctions.php';
}
require_once __DIR__ . '/rackflow.php';
require_once __DIR__ . '/git_update.php';

if (empty($_SESSION['adminid'])) {
    http_response_code(403);
    header('Content-Type: text/plain; charset=utf-8');
    echo 'Admin login required.';
    exit;
}

$html = rackflow_gitupdate_adminPageHtml(rackflow_gitupdateActionUrl());

if (class_exists('WHMCS\\Admin')) {
    try {
        $admin = new \WHMCS\Admin('');
        $admin->title = 'RackFlow Git Updates';
        $admin->sidebar = 'config';
        $admin->icon = 'autosettings';
        $admin->pagetitle = 'RackFlow Git Updates';
        $admin->content = $html;
        $admin->display();
        exit;
    } catch (Throwable $e) {
        rackflow_gitupdate_log('git update admin chrome failed', array('error' => $e->getMessage()));
    }
}

header('Content-Type: text/html; charset=utf-8');
header('X-Robots-Tag: noindex, nofollow');
header('Cache-Control: no-store');
?>
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>RackFlow Git Updates</title>
</head>
<body style="margin:0;background:#eef2f6;padding:24px;">
<?php echo $html; ?>
</body>
</html>
