<?php
/**
 * WHMCS addon wrapper so git updates appear under Addons with admin chrome.
 */

if (!defined('WHMCS')) {
    die('This file cannot be accessed directly');
}

function rackflow_updater_config()
{
    return array(
        'name' => 'RackFlow Git Updates',
        'description' => 'Download the latest RackFlow provisioning module files from git and apply them on this WHMCS server.',
        'version' => '1.0.0',
        'author' => 'RackFlow',
        'language' => 'english',
    );
}

function rackflow_updater_activate()
{
    return array('status' => 'success', 'description' => 'RackFlow git updates activated.');
}

function rackflow_updater_deactivate()
{
    return array('status' => 'success');
}

function rackflow_updater_output($vars)
{
    unset($vars);
    $module = ROOTDIR . '/modules/servers/rackflow/rackflow.php';
    $lib = ROOTDIR . '/modules/servers/rackflow/git_update.php';
    if (!is_file($module) || !is_file($lib)) {
        echo '<div class="errorbox">The RackFlow server module is not installed on this WHMCS instance.</div>';
        return;
    }
    require_once $module;
    require_once $lib;
    echo rackflow_gitupdate_adminPageHtml(rackflow_gitupdateActionUrl());
}
