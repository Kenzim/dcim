<?php
/**
 * Admin UI: pull RackFlow module files from git.
 *
 * Requires an authenticated WHMCS admin session. When the RackFlow Git Updates
 * addon is active, this file redirects into addonmodules.php so WHMCS can
 * serve Blend CSS/JS from /admin/. Instantiating WHMCS\Admin from this
 * /modules/servers/rackflow/ URL makes those assets 404.
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

if (rackflow_gitupdate_addonIsActive()) {
    $dest = rackflow_gitupdateAdminEntryUrl();
    header('Location: ' . $dest, true, 302);
    header('X-Robots-Tag: noindex, nofollow');
    header('Cache-Control: no-store');
    exit;
}

$html = rackflow_gitupdate_adminPageHtml(rackflow_gitupdateActionUrl(), false);

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
  <style>
    body { margin: 0; background: #eef2f6; padding: 24px; font-family: system-ui, sans-serif; color: #1c2430; }
    .form-control { display: block; width: 100%; height: 34px; padding: 6px 10px; border: 1px solid #ccc; border-radius: 4px; box-sizing: border-box; }
    .btn { display: inline-block; padding: 6px 12px; border: 1px solid #ccc; border-radius: 4px; background: #fff; cursor: pointer; }
    .btn-primary { background: #337ab7; border-color: #2e6da4; color: #fff; }
    .help-block { display: block; margin-top: 5px; color: #737373; font-size: 12px; }
    .well { background: #f5f5f5; border: 1px solid #e3e3e3; border-radius: 4px; padding: 12px; }
    .checkbox { margin: 8px 0; }
    .row:after { content: ""; display: table; clear: both; }
    .col-sm-4, .col-sm-8 { box-sizing: border-box; }
    @media (min-width: 768px) {
      .col-sm-4 { float: left; width: 33.333%; padding-right: 8px; }
      .col-sm-8 { float: left; width: 66.666%; padding-left: 8px; }
    }
  </style>
</head>
<body>
<?php echo $html; ?>
</body>
</html>
