<?php
/**
 * Pull RackFlow WHMCS module files from GitHub or Forgejo/Gitea.
 *
 * Downloads a branch archive (not a local git pull) and syncs
 * whmcs/modules/servers/rackflow into this module directory.
 */

if (!defined('WHMCS')) {
    die('This file cannot be accessed directly');
}

function rackflow_gitupdate_log($message, array $context = array())
{
    if (function_exists('rackflow_log')) {
        rackflow_log($message, $context);
    }
}

/**
 * @param string $systemUrl
 * @return string
 */
function rackflow_gitupdatePageUrl($systemUrl = '')
{
    return rackflow_gitupdate_moduleFileUrl('module_update.php', $systemUrl);
}

/**
 * Addon page when the updater addon is active; otherwise the standalone module page.
 *
 * @param string $systemUrl
 * @return string
 */
function rackflow_gitupdateAdminEntryUrl($systemUrl = '')
{
    if (rackflow_gitupdate_addonIsActive()) {
        $basePath = function_exists('rackflow_whmcsUrlBasePath') ? rackflow_whmcsUrlBasePath() : null;
        if ($basePath === null) {
            $basePath = '';
            if ($systemUrl !== '') {
                $path = (string)parse_url($systemUrl, PHP_URL_PATH);
                $basePath = rtrim($path, '/');
            }
        }
        return $basePath . '/' . rackflow_gitupdate_adminFolderName() . '/addonmodules.php?module=rackflow_updater';
    }
    return rackflow_gitupdatePageUrl($systemUrl);
}

/**
 * @return bool
 */
function rackflow_gitupdate_addonIsActive()
{
    if (!class_exists('\Illuminate\Database\Capsule\Manager')) {
        return false;
    }
    try {
        $row = \Illuminate\Database\Capsule\Manager::table('tbladdonmodules')
            ->where('module', 'rackflow_updater')
            ->first();
        return (bool)$row;
    } catch (Exception $e) {
        return false;
    }
}

/**
 * @return string
 */
function rackflow_gitupdate_adminFolderName()
{
    if (isset($GLOBALS['whmcs']) && is_object($GLOBALS['whmcs']) && method_exists($GLOBALS['whmcs'], 'get_admin_folder_name')) {
        $name = $GLOBALS['whmcs']->get_admin_folder_name();
        if (is_string($name) && $name !== '') {
            return $name;
        }
    }
    if (!empty($GLOBALS['customadminpath'])) {
        return (string)$GLOBALS['customadminpath'];
    }
    return 'admin';
}

/**
 * @param string $systemUrl
 * @return string
 */
function rackflow_gitupdateActionUrl($systemUrl = '')
{
    return rackflow_gitupdate_moduleFileUrl('module_update_action.php', $systemUrl);
}

/**
 * @param string $file
 * @param string $systemUrl
 * @return string
 */
function rackflow_gitupdate_moduleFileUrl($file, $systemUrl = '')
{
    $file = ltrim((string)$file, '/');
    $basePath = function_exists('rackflow_whmcsUrlBasePath') ? rackflow_whmcsUrlBasePath() : null;
    if ($basePath === null) {
        $basePath = '';
        if ($systemUrl !== '') {
            $path = (string)parse_url($systemUrl, PHP_URL_PATH);
            $basePath = rtrim($path, '/');
        }
    }
    return $basePath . '/modules/servers/rackflow/' . $file;
}

/**
 * @return string
 */
function rackflow_gitupdate_moduleDir()
{
    return __DIR__;
}

/**
 * @return string|false
 */
function rackflow_gitupdate_whmcsRoot()
{
    return realpath(__DIR__ . '/../../../');
}

/**
 * @return array{ok:bool,error:?string,provider:?string,origin:?string,owner:?string,repo:?string}
 */
function rackflow_gitupdate_parseRepoUrl($url)
{
    $url = trim((string)$url);
    $fail = array(
        'ok' => false,
        'error' => 'Repository URL must look like https://github.com/owner/repo or https://git.example/owner/repo.',
        'provider' => null,
        'origin' => null,
        'owner' => null,
        'repo' => null,
    );
    if ($url === '') {
        $fail['error'] = 'Repository URL is required.';
        return $fail;
    }

    if (preg_match('#^git@github\.com:([^/]+)/([^/]+?)$#i', $url, $m)) {
        return rackflow_gitupdate_repoSpec('github', 'https://github.com', $m[1], $m[2]);
    }
    if (preg_match('#^git@([^:]+):([^/]+)/([^/]+?)$#', $url, $m)) {
        return rackflow_gitupdate_repoSpec('gitea', 'http://' . $m[1], $m[2], $m[3]);
    }
    if (preg_match('#^ssh://git@([^/]+)/([^/]+)/([^/]+?)$#', $url, $m)) {
        $host = $m[1];
        $provider = (strcasecmp($host, 'github.com') === 0) ? 'github' : 'gitea';
        $scheme = ($provider === 'github') ? 'https' : 'http';
        return rackflow_gitupdate_repoSpec($provider, $scheme . '://' . $host, $m[2], $m[3]);
    }

    $parts = parse_url($url);
    if (!is_array($parts) || empty($parts['host'])) {
        return $fail;
    }
    $path = isset($parts['path']) ? trim((string)$parts['path'], '/') : '';
    $path = preg_replace('/\.git$/i', '', $path);
    $segments = array_values(array_filter(explode('/', $path), 'strlen'));
    if (count($segments) < 2) {
        return $fail;
    }
    $host = (string)$parts['host'];
    $scheme = isset($parts['scheme']) ? strtolower((string)$parts['scheme']) : 'https';
    if ($scheme !== 'http' && $scheme !== 'https') {
        return $fail;
    }
    $origin = $scheme . '://' . $host;
    if (!empty($parts['port'])) {
        $origin .= ':' . (int)$parts['port'];
    }
    $provider = (strcasecmp($host, 'github.com') === 0 || strcasecmp($host, 'www.github.com') === 0)
        ? 'github'
        : 'gitea';
    return rackflow_gitupdate_repoSpec($provider, $origin, $segments[0], $segments[1]);
}

/**
 * @param string $provider
 * @param string $origin
 * @param string $owner
 * @param string $repo
 * @return array
 */
function rackflow_gitupdate_repoSpec($provider, $origin, $owner, $repo)
{
    $owner = rawurldecode((string)$owner);
    $repo = preg_replace('/\.git$/i', '', rawurldecode((string)$repo));
    if ($owner === '.' || $owner === '..' || $repo === '.' || $repo === '..') {
        return array(
            'ok' => false,
            'error' => 'Repository owner/name contains unsupported characters.',
            'provider' => null,
            'origin' => null,
            'owner' => null,
            'repo' => null,
        );
    }
    if (!preg_match('/^[A-Za-z0-9._-]+$/', $owner) || !preg_match('/^[A-Za-z0-9._-]+$/', $repo)) {
        return array(
            'ok' => false,
            'error' => 'Repository owner/name contains unsupported characters.',
            'provider' => null,
            'origin' => null,
            'owner' => null,
            'repo' => null,
        );
    }
    return array(
        'ok' => true,
        'error' => null,
        'provider' => $provider,
        'origin' => $origin,
        'owner' => $owner,
        'repo' => $repo,
    );
}

/**
 * @param array $repo
 * @param string $branch
 * @return string
 */
function rackflow_gitupdate_branchApiUrl(array $repo, $branch)
{
    $branch = rackflow_gitupdate_normalizeBranch($branch);
    $enc = str_replace('%2F', '/', rawurlencode($branch));
    if ($repo['provider'] === 'github') {
        return 'https://api.github.com/repos/' . rawurlencode($repo['owner']) . '/' . rawurlencode($repo['repo']) . '/branches/' . $enc;
    }
    return rtrim($repo['origin'], '/') . '/api/v1/repos/' . rawurlencode($repo['owner']) . '/' . rawurlencode($repo['repo']) . '/branches/' . $enc;
}

/**
 * @param array $repo
 * @param string $branch
 * @return string
 */
function rackflow_gitupdate_archiveUrl(array $repo, $branch)
{
    $branch = rackflow_gitupdate_normalizeBranch($branch);
    $enc = str_replace('%2F', '/', rawurlencode($branch));
    if ($repo['provider'] === 'github') {
        return 'https://api.github.com/repos/' . rawurlencode($repo['owner']) . '/' . rawurlencode($repo['repo']) . '/zipball/' . $enc;
    }
    return rtrim($repo['origin'], '/') . '/api/v1/repos/' . rawurlencode($repo['owner']) . '/' . rawurlencode($repo['repo']) . '/archive/' . $enc . '.zip';
}

/**
 * @param string $branch
 * @return string
 */
function rackflow_gitupdate_normalizeBranch($branch)
{
    $branch = trim((string)$branch);
    if ($branch === '') {
        return 'main';
    }
    return $branch;
}

/**
 * @param string $name Zip entry name
 * @return bool
 */
function rackflow_gitupdate_isSafeZipName($name)
{
    $name = str_replace('\\', '/', (string)$name);
    if ($name === '' || $name[0] === '/') {
        return false;
    }
    if (strpos($name, "\0") !== false) {
        return false;
    }
    $parts = explode('/', $name);
    foreach ($parts as $part) {
        if ($part === '..') {
            return false;
        }
    }
    return true;
}

/**
 * Locate the RackFlow server-module directory inside an extracted (or zip) tree.
 *
 * @param string $root
 * @return string|null Absolute path
 */
function rackflow_gitupdate_findExtractedModuleDir($root)
{
    $root = rtrim((string)$root, '/');
    if ($root === '' || !is_dir($root)) {
        return null;
    }
    $direct = $root . '/whmcs/modules/servers/rackflow';
    if (is_file($direct . '/rackflow.php')) {
        return $direct;
    }
    $iterator = new RecursiveIteratorIterator(
        new RecursiveDirectoryIterator($root, FilesystemIterator::SKIP_DOTS),
        RecursiveIteratorIterator::SELF_FIRST
    );
    $iterator->setMaxDepth(8);
    foreach ($iterator as $item) {
        if (!$item->isFile()) {
            continue;
        }
        if ($item->getFilename() !== 'rackflow.php') {
            continue;
        }
        $dir = $item->getPath();
        $normalized = str_replace('\\', '/', $dir);
        if (substr($normalized, -strlen('/whmcs/modules/servers/rackflow')) === '/whmcs/modules/servers/rackflow'
            || substr($normalized, -strlen('/modules/servers/rackflow')) === '/modules/servers/rackflow'
        ) {
            return $dir;
        }
        if (is_file($dir . '/hooks.php') && is_file($dir . '/whmcs.json')) {
            return $dir;
        }
    }
    return null;
}

/**
 * Locate the reseller module directory next to an extracted RackFlow module dir.
 *
 * @param string $rackflowDir
 * @return string|null
 */
function rackflow_gitupdate_findExtractedResellerDir($rackflowDir)
{
    $sibling = dirname((string)$rackflowDir) . '/rackflow_reseller';
    if (is_file($sibling . '/rackflow_reseller.php')) {
        return $sibling;
    }
    return null;
}

/**
 * Files that must exist in a git archive before any on-disk files are replaced.
 * Without this, updating to a commit from before the updater existed deletes
 * git_update.php and the admin action endpoint.
 *
 * @return array
 */
function rackflow_gitupdate_requiredModuleFiles()
{
    return array(
        'rackflow.php',
        'hooks.php',
        'git_update.php',
        'module_update.php',
        'module_update_action.php',
    );
}

/**
 * @param string $sourceModule
 * @return array{ok:bool,error:?string}
 */
function rackflow_gitupdate_validateIncomingModule($sourceModule)
{
    $sourceModule = rtrim((string)$sourceModule, '/');
    foreach (rackflow_gitupdate_requiredModuleFiles() as $rel) {
        if (!is_file($sourceModule . '/' . $rel)) {
            return array(
                'ok' => false,
                'error' => 'Git archive is missing ' . $rel . '. Refusing to apply so this updater is not removed. Push/commit the updater first, or pick a newer branch.',
            );
        }
    }
    return array('ok' => true, 'error' => null);
}

/**
 * Recursively copy $src onto $dst (files only), deleting dest files not in src.
 *
 * @param string $src
 * @param string $dst
 * @return array{ok:bool,error:?string,copied:int,deleted:int,files:array}
 */
function rackflow_gitupdate_syncDirectory($src, $dst)
{
    $src = realpath($src);
    if ($src === false || !is_dir($src)) {
        return array('ok' => false, 'error' => 'Extracted module directory is missing.', 'copied' => 0, 'deleted' => 0, 'files' => array());
    }
    $dst = rtrim((string)$dst, '/');
    if ($dst === '' || !is_dir($dst)) {
        return array('ok' => false, 'error' => 'Destination module directory is missing or not writable.', 'copied' => 0, 'deleted' => 0, 'files' => array());
    }
    if (!is_writable($dst)) {
        return array('ok' => false, 'error' => 'Module directory is not writable by the web user. chown it to the PHP user and retry.', 'copied' => 0, 'deleted' => 0, 'files' => array());
    }

    $srcFiles = rackflow_gitupdate_listRelativeFiles($src);
    $dstFiles = rackflow_gitupdate_listRelativeFiles($dst);
    $copied = 0;
    $deleted = 0;
    $changed = array();

    foreach ($srcFiles as $rel) {
        if (rackflow_gitupdate_shouldSkipRel($rel)) {
            continue;
        }
        $from = $src . '/' . $rel;
        $to = $dst . '/' . $rel;
        $toDir = dirname($to);
        if (!is_dir($toDir) && !@mkdir($toDir, 0755, true)) {
            return array('ok' => false, 'error' => 'Could not create ' . $rel, 'copied' => $copied, 'deleted' => $deleted, 'files' => $changed);
        }
        $same = is_file($to) && @md5_file($from) === @md5_file($to);
        if ($same) {
            continue;
        }
        if (!@copy($from, $to)) {
            return array('ok' => false, 'error' => 'Failed to write ' . $rel, 'copied' => $copied, 'deleted' => $deleted, 'files' => $changed);
        }
        @chmod($to, 0644);
        $copied++;
        $changed[] = $rel;
    }

    foreach ($dstFiles as $rel) {
        if (rackflow_gitupdate_shouldSkipRel($rel)) {
            continue;
        }
        if (isset($srcFiles[$rel])) {
            continue;
        }
        $path = $dst . '/' . $rel;
        if (is_file($path) && @unlink($path)) {
            $deleted++;
            $changed[] = $rel . ' (removed)';
        }
    }

    rackflow_gitupdate_removeEmptyDirs($dst, $src);

    return array('ok' => true, 'error' => null, 'copied' => $copied, 'deleted' => $deleted, 'files' => $changed);
}

/**
 * @param string $rel
 * @return bool
 */
function rackflow_gitupdate_shouldSkipRel($rel)
{
    $rel = str_replace('\\', '/', (string)$rel);
    if ($rel === '' || $rel[0] === '.') {
        return true;
    }
    if (strpos($rel, '/.') !== false) {
        return true;
    }
    return false;
}

/**
 * @param string $dir
 * @return array<string,string> relative path => relative path
 */
function rackflow_gitupdate_listRelativeFiles($dir)
{
    $dir = rtrim((string)$dir, '/');
    $out = array();
    if (!is_dir($dir)) {
        return $out;
    }
    $iterator = new RecursiveIteratorIterator(
        new RecursiveDirectoryIterator($dir, FilesystemIterator::SKIP_DOTS)
    );
    foreach ($iterator as $item) {
        if (!$item->isFile()) {
            continue;
        }
        $full = $item->getPathname();
        $rel = substr($full, strlen($dir) + 1);
        $rel = str_replace('\\', '/', $rel);
        $out[$rel] = $rel;
    }
    return $out;
}

/**
 * @param string $dst
 * @param string $src
 * @return void
 */
function rackflow_gitupdate_removeEmptyDirs($dst, $src)
{
    $dst = rtrim($dst, '/');
    $src = rtrim($src, '/');
    $iterator = new RecursiveIteratorIterator(
        new RecursiveDirectoryIterator($dst, FilesystemIterator::SKIP_DOTS),
        RecursiveIteratorIterator::CHILD_FIRST
    );
    foreach ($iterator as $item) {
        if (!$item->isDir()) {
            continue;
        }
        $full = $item->getPathname();
        $rel = substr($full, strlen($dst) + 1);
        $rel = str_replace('\\', '/', $rel);
        if (rackflow_gitupdate_shouldSkipRel($rel)) {
            continue;
        }
        if (is_dir($src . '/' . $rel)) {
            continue;
        }
        @rmdir($full);
    }
}

/**
 * @param array $json
 * @param string $provider
 * @return array{ok:bool,error:?string,sha:?string,message:?string,date:?string}
 */
function rackflow_gitupdate_normalizeBranchPayload(array $json, $provider)
{
    if ($provider === 'github') {
        $sha = '';
        if (isset($json['commit']['sha'])) {
            $sha = (string)$json['commit']['sha'];
        }
        $message = '';
        if (isset($json['commit']['commit']['message'])) {
            $message = (string)$json['commit']['commit']['message'];
        }
        $date = '';
        if (isset($json['commit']['commit']['committer']['date'])) {
            $date = (string)$json['commit']['commit']['committer']['date'];
        } elseif (isset($json['commit']['commit']['author']['date'])) {
            $date = (string)$json['commit']['commit']['author']['date'];
        }
        if ($sha === '') {
            return array('ok' => false, 'error' => 'GitHub branch response did not include a commit sha.', 'sha' => null, 'message' => null, 'date' => null);
        }
        return array('ok' => true, 'error' => null, 'sha' => $sha, 'message' => trim($message), 'date' => $date);
    }
    $sha = '';
    if (isset($json['commit']['id'])) {
        $sha = (string)$json['commit']['id'];
    } elseif (isset($json['commit']['sha'])) {
        $sha = (string)$json['commit']['sha'];
    }
    $message = '';
    if (isset($json['commit']['message'])) {
        $message = (string)$json['commit']['message'];
    }
    $date = '';
    if (isset($json['commit']['timestamp'])) {
        $date = (string)$json['commit']['timestamp'];
    } elseif (isset($json['commit']['created'])) {
        $date = (string)$json['commit']['created'];
    }
    if ($sha === '') {
        return array('ok' => false, 'error' => 'Git branch response did not include a commit id.', 'sha' => null, 'message' => null, 'date' => null);
    }
    return array('ok' => true, 'error' => null, 'sha' => $sha, 'message' => trim($message), 'date' => $date);
}

/**
 * @param string $url
 * @param string $token
 * @param string $provider
 * @param bool $insecureTls
 * @param string|null $destFile
 * @param int $timeoutSeconds
 * @return array{ok:bool,error:?string,http_code:int,body:?string}
 */
function rackflow_gitupdate_httpGet($url, $token, $provider, $insecureTls, $destFile = null, $timeoutSeconds = 60)
{
    $timeoutSeconds = (int)$timeoutSeconds;
    if ($timeoutSeconds < 5) {
        $timeoutSeconds = 5;
    }
    $headers = array(
        'Accept: ' . ($destFile !== null
            ? 'application/octet-stream, application/zip, */*'
            : 'application/vnd.github+json, application/json'),
        'User-Agent: RackFlow-WHMCS-Updater',
    );
    $token = trim((string)$token);
    if ($token !== '') {
        if ($provider === 'github') {
            $headers[] = 'Authorization: Bearer ' . $token;
        } else {
            $headers[] = 'Authorization: token ' . $token;
        }
    }

    $fp = null;
    $maxBytes = 80 * 1024 * 1024;
    $written = 0;
    $ch = curl_init();
    curl_setopt($ch, CURLOPT_URL, $url);
    curl_setopt($ch, CURLOPT_FOLLOWLOCATION, true);
    curl_setopt($ch, CURLOPT_MAXREDIRS, 5);
    curl_setopt($ch, CURLOPT_CONNECTTIMEOUT, 15);
    curl_setopt($ch, CURLOPT_TIMEOUT, $timeoutSeconds);
    curl_setopt($ch, CURLOPT_HTTPHEADER, $headers);
    curl_setopt($ch, CURLOPT_SSL_VERIFYPEER, $insecureTls ? false : true);
    curl_setopt($ch, CURLOPT_SSL_VERIFYHOST, $insecureTls ? 0 : 2);

    if ($destFile !== null) {
        $fp = fopen($destFile, 'wb');
        if ($fp === false) {
            curl_close($ch);
            return array('ok' => false, 'error' => 'Could not open a temporary file for the git archive.', 'http_code' => 0, 'body' => null);
        }
        curl_setopt($ch, CURLOPT_WRITEFUNCTION, function ($chHandle, $data) use (&$written, $maxBytes, $fp) {
            unset($chHandle);
            $len = strlen($data);
            $written += $len;
            if ($written > $maxBytes) {
                return 0;
            }
            return fwrite($fp, $data);
        });
    } else {
        curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
    }

    $response = curl_exec($ch);
    $httpCode = (int)curl_getinfo($ch, CURLINFO_HTTP_CODE);
    $curlError = curl_error($ch);
    curl_close($ch);
    if (is_resource($fp)) {
        fclose($fp);
    }

    if ($curlError) {
        if ($destFile && is_file($destFile)) {
            @unlink($destFile);
        }
        return array('ok' => false, 'error' => 'CURL error: ' . $curlError, 'http_code' => $httpCode, 'body' => null);
    }
    if ($httpCode < 200 || $httpCode >= 300) {
        if ($destFile && is_file($destFile)) {
            @unlink($destFile);
        }
        $hint = 'HTTP ' . $httpCode;
        if (is_string($response) && $response !== '') {
            $decoded = json_decode($response, true);
            if (is_array($decoded) && isset($decoded['message'])) {
                $hint .= ': ' . $decoded['message'];
            }
        }
        if ($httpCode === 404) {
            $hint .= ' (repo/branch not found, or this host cannot reach the git HTTP API)';
        }
        if ($httpCode === 401 || $httpCode === 403) {
            $hint .= ' (add a git access token with repo read permission)';
        }
        return array('ok' => false, 'error' => $hint, 'http_code' => $httpCode, 'body' => is_string($response) ? $response : null);
    }
    return array('ok' => true, 'error' => null, 'http_code' => $httpCode, 'body' => $destFile === null ? (is_string($response) ? $response : '') : null);
}

/**
 * @param string $zipPath
 * @param string $destDir
 * @return array{ok:bool,error:?string}
 */
function rackflow_gitupdate_extractZip($zipPath, $destDir)
{
    if (!class_exists('ZipArchive')) {
        return array('ok' => false, 'error' => 'PHP zip extension is required to unpack the git archive.');
    }
    $zip = new ZipArchive();
    $opened = $zip->open($zipPath);
    if ($opened !== true) {
        return array('ok' => false, 'error' => 'Could not open git archive (zip error ' . $opened . ').');
    }
    for ($i = 0; $i < $zip->numFiles; $i++) {
        $name = $zip->getNameIndex($i);
        if (!is_string($name) || $name === '' || substr($name, -1) === '/') {
            continue;
        }
        if (!rackflow_gitupdate_isSafeZipName($name)) {
            $zip->close();
            return array('ok' => false, 'error' => 'Git archive contained an unsafe path and was rejected.');
        }
        $stat = $zip->statIndex($i);
        if (is_array($stat) && isset($stat['size']) && (int)$stat['size'] > 20 * 1024 * 1024) {
            $zip->close();
            return array('ok' => false, 'error' => 'Git archive contained a file larger than 20MB and was rejected.');
        }
    }
    if (!$zip->extractTo($destDir)) {
        $zip->close();
        return array('ok' => false, 'error' => 'Failed to extract the git archive.');
    }
    $zip->close();
    return array('ok' => true, 'error' => null);
}

/**
 * @param string $name
 * @param string $default
 * @return string
 */
function rackflow_gitupdate_getSetting($name, $default = '')
{
    if (!class_exists('\Illuminate\Database\Capsule\Manager')) {
        return $default;
    }
    try {
        $row = \Illuminate\Database\Capsule\Manager::table('tblconfiguration')
            ->where('setting', $name)
            ->first();
        if ($row && isset($row->value)) {
            return (string)$row->value;
        }
    } catch (Exception $e) {
        rackflow_gitupdate_log('git update setting read failed', array('setting' => $name, 'error' => $e->getMessage()));
    }
    return $default;
}

/**
 * @param string $name
 * @param string $value
 * @return bool
 */
function rackflow_gitupdate_setSetting($name, $value)
{
    if (!class_exists('\Illuminate\Database\Capsule\Manager')) {
        return false;
    }
    $now = date('Y-m-d H:i:s');
    try {
        $row = \Illuminate\Database\Capsule\Manager::table('tblconfiguration')->where('setting', $name)->first();
        if ($row) {
            \Illuminate\Database\Capsule\Manager::table('tblconfiguration')->where('setting', $name)->update(array(
                'value' => (string)$value,
                'updated_at' => $now,
            ));
            return true;
        }
        \Illuminate\Database\Capsule\Manager::table('tblconfiguration')->insert(array(
            'setting' => $name,
            'value' => (string)$value,
            'created_at' => $now,
            'updated_at' => $now,
        ));
        return true;
    } catch (Exception $e) {
        rackflow_gitupdate_log('git update setting write failed', array('setting' => $name, 'error' => $e->getMessage()));
        return false;
    }
}

/**
 * @param string $value
 * @return string
 */
function rackflow_gitupdate_encrypt($value)
{
    $value = (string)$value;
    if ($value === '') {
        return '';
    }
    if (function_exists('encrypt')) {
        return (string)encrypt($value);
    }
    return $value;
}

/**
 * @param string $value
 * @return string
 */
function rackflow_gitupdate_decrypt($value)
{
    $value = (string)$value;
    if ($value === '') {
        return '';
    }
    if (function_exists('decrypt')) {
        $out = decrypt($value);
        if (is_string($out) && $out !== '') {
            return $out;
        }
    }
    return $value;
}

/**
 * @return array
 */
function rackflow_gitupdate_loadSettings()
{
    $token = rackflow_gitupdate_decrypt(rackflow_gitupdate_getSetting('RackflowGitUpdateToken', ''));
    return array(
        'repo_url' => rackflow_gitupdate_getSetting('RackflowGitUpdateRepo', 'https://github.com/Kenzim/dcim'),
        'branch' => rackflow_gitupdate_normalizeBranch(rackflow_gitupdate_getSetting('RackflowGitUpdateBranch', 'main')),
        'token' => $token,
        'token_set' => $token !== '',
        'insecure_tls' => rackflow_gitupdate_getSetting('RackflowGitUpdateInsecure', '') === '1',
        'update_reseller' => rackflow_gitupdate_getSetting('RackflowGitUpdateReseller', '1') !== '0',
        'last_sha' => rackflow_gitupdate_getSetting('RackflowGitUpdateLastSha', ''),
        'last_message' => rackflow_gitupdate_getSetting('RackflowGitUpdateLastMessage', ''),
        'last_at' => rackflow_gitupdate_getSetting('RackflowGitUpdateLastAt', ''),
    );
}

/**
 * @param array $input
 * @return array
 */
function rackflow_gitupdate_saveSettings(array $input)
{
    $repoUrl = isset($input['repo_url']) ? trim((string)$input['repo_url']) : '';
    $branch = rackflow_gitupdate_normalizeBranch(isset($input['branch']) ? $input['branch'] : 'main');
    $insecure = !empty($input['insecure_tls']);
    $updateReseller = !empty($input['update_reseller']);
    $parsed = rackflow_gitupdate_parseRepoUrl($repoUrl);
    if (!$parsed['ok']) {
        return array('ok' => false, 'error' => $parsed['error']);
    }
    rackflow_gitupdate_setSetting('RackflowGitUpdateRepo', $repoUrl);
    rackflow_gitupdate_setSetting('RackflowGitUpdateBranch', $branch);
    rackflow_gitupdate_setSetting('RackflowGitUpdateInsecure', $insecure ? '1' : '0');
    rackflow_gitupdate_setSetting('RackflowGitUpdateReseller', $updateReseller ? '1' : '0');
    if (array_key_exists('token', $input)) {
        $token = trim((string)$input['token']);
        if ($token === '' && empty($input['clear_token'])) {
            // leave existing token
        } elseif (!empty($input['clear_token'])) {
            rackflow_gitupdate_setSetting('RackflowGitUpdateToken', '');
        } else {
            rackflow_gitupdate_setSetting('RackflowGitUpdateToken', rackflow_gitupdate_encrypt($token));
        }
    }
    return array('ok' => true, 'error' => null);
}

/**
 * @return array
 */
function rackflow_gitupdate_statusPayload()
{
    $settings = rackflow_gitupdate_loadSettings();
    $dir = rackflow_gitupdate_moduleDir();
    $resellerDir = dirname($dir) . '/rackflow_reseller';
    unset($settings['token']);
    $settings['module_dir'] = $dir;
    $settings['module_writable'] = is_dir($dir) && is_writable($dir);
    $settings['reseller_installed'] = is_file($resellerDir . '/rackflow_reseller.php');
    $settings['reseller_writable'] = $settings['reseller_installed'] && is_writable($resellerDir);
    $settings['zip_available'] = class_exists('ZipArchive');
    return $settings;
}

/**
 * @param array $settings
 * @return array
 */
function rackflow_gitupdate_checkRemote(array $settings)
{
    $parsed = rackflow_gitupdate_parseRepoUrl($settings['repo_url']);
    if (!$parsed['ok']) {
        return array('ok' => false, 'error' => $parsed['error']);
    }
    $url = rackflow_gitupdate_branchApiUrl($parsed, $settings['branch']);
    $http = rackflow_gitupdate_httpGet(
        $url,
        isset($settings['token']) ? $settings['token'] : '',
        $parsed['provider'],
        !empty($settings['insecure_tls']),
        null,
        30
    );
    if (!$http['ok']) {
        return array('ok' => false, 'error' => $http['error']);
    }
    $json = json_decode((string)$http['body'], true);
    if (!is_array($json)) {
        return array('ok' => false, 'error' => 'Git API did not return JSON for the branch.');
    }
    $norm = rackflow_gitupdate_normalizeBranchPayload($json, $parsed['provider']);
    if (!$norm['ok']) {
        return $norm;
    }
    $lastSha = isset($settings['last_sha']) ? (string)$settings['last_sha'] : '';
    $norm['up_to_date'] = ($lastSha !== '' && hash_equals($lastSha, $norm['sha']));
    $norm['provider'] = $parsed['provider'];
    $norm['repo'] = $parsed['owner'] . '/' . $parsed['repo'];
    $norm['branch'] = rackflow_gitupdate_normalizeBranch($settings['branch']);
    return $norm;
}

/**
 * @param string $dir
 */
function rackflow_gitupdate_rrmdir($dir)
{
    $dir = (string)$dir;
    if ($dir === '' || !is_dir($dir)) {
        return;
    }
    $iterator = new RecursiveIteratorIterator(
        new RecursiveDirectoryIterator($dir, FilesystemIterator::SKIP_DOTS),
        RecursiveIteratorIterator::CHILD_FIRST
    );
    foreach ($iterator as $item) {
        if ($item->isDir()) {
            @rmdir($item->getPathname());
        } else {
            @unlink($item->getPathname());
        }
    }
    @rmdir($dir);
}

/**
 * @param array $settings
 * @return array
 */
function rackflow_gitupdate_apply(array $settings)
{
    $parsed = rackflow_gitupdate_parseRepoUrl($settings['repo_url']);
    if (!$parsed['ok']) {
        return array('ok' => false, 'error' => $parsed['error']);
    }
    if (!class_exists('ZipArchive')) {
        return array('ok' => false, 'error' => 'PHP zip extension is required.');
    }
    $dest = rackflow_gitupdate_moduleDir();
    if (!is_writable($dest)) {
        return array('ok' => false, 'error' => 'Module directory is not writable by the web user.');
    }

    $remote = rackflow_gitupdate_checkRemote($settings);
    if (!$remote['ok']) {
        return $remote;
    }

    $tmp = sys_get_temp_dir() . '/rackflow-git-update-' . uniqid('', true);
    if (!@mkdir($tmp, 0700, true)) {
        return array('ok' => false, 'error' => 'Could not create a temporary directory.');
    }
    $zipPath = $tmp . '/archive.zip';
    $extractDir = $tmp . '/extract';
    @mkdir($extractDir, 0700, true);

    try {
        $archiveUrl = rackflow_gitupdate_archiveUrl($parsed, $settings['branch']);
        $http = rackflow_gitupdate_httpGet(
            $archiveUrl,
            isset($settings['token']) ? $settings['token'] : '',
            $parsed['provider'],
            !empty($settings['insecure_tls']),
            $zipPath,
            120
        );
        if (!$http['ok']) {
            return array('ok' => false, 'error' => $http['error']);
        }
        $extracted = rackflow_gitupdate_extractZip($zipPath, $extractDir);
        if (!$extracted['ok']) {
            return $extracted;
        }
        $sourceModule = rackflow_gitupdate_findExtractedModuleDir($extractDir);
        if ($sourceModule === null) {
            return array('ok' => false, 'error' => 'Archive did not contain whmcs/modules/servers/rackflow.');
        }
        $valid = rackflow_gitupdate_validateIncomingModule($sourceModule);
        if (!$valid['ok']) {
            return $valid;
        }
        $sync = rackflow_gitupdate_syncDirectory($sourceModule, $dest);
        if (!$sync['ok']) {
            return $sync;
        }

        $resellerSync = null;
        $resellerDest = dirname($dest) . '/rackflow_reseller';
        $wantReseller = !empty($settings['update_reseller']) && is_file($resellerDest . '/rackflow_reseller.php');
        if ($wantReseller) {
            $sourceReseller = rackflow_gitupdate_findExtractedResellerDir($sourceModule);
            if ($sourceReseller === null) {
                $resellerSync = array('ok' => true, 'skipped' => true, 'error' => 'Archive had no reseller module; left the installed copy unchanged.');
            } elseif (!is_writable($resellerDest)) {
                $resellerSync = array('ok' => false, 'error' => 'Reseller module is installed but not writable.');
            } else {
                $resellerSync = rackflow_gitupdate_syncDirectory($sourceReseller, $resellerDest);
                if (!$resellerSync['ok']) {
                    return array('ok' => false, 'error' => 'RackFlow module updated but reseller sync failed: ' . $resellerSync['error'], 'rackflow' => $sync, 'reseller' => $resellerSync);
                }
            }
        }

        $message = isset($remote['message']) ? $remote['message'] : '';
        $parts = preg_split("/\r\n|\n|\r/", $message);
        $shortMessage = is_array($parts) && isset($parts[0]) ? $parts[0] : $message;
        rackflow_gitupdate_setSetting('RackflowGitUpdateLastSha', (string)$remote['sha']);
        rackflow_gitupdate_setSetting('RackflowGitUpdateLastMessage', substr((string)$shortMessage, 0, 250));
        rackflow_gitupdate_setSetting('RackflowGitUpdateLastAt', date('Y-m-d H:i:s'));
        if (function_exists('opcache_reset')) {
            @opcache_reset();
        }
        rackflow_gitupdate_log('git module update applied', array(
            'sha' => $remote['sha'],
            'copied' => $sync['copied'],
            'deleted' => $sync['deleted'],
        ));
        return array(
            'ok' => true,
            'error' => null,
            'sha' => $remote['sha'],
            'message' => $shortMessage,
            'rackflow' => $sync,
            'reseller' => $resellerSync,
        );
    } finally {
        rackflow_gitupdate_rrmdir($tmp);
    }
}

/**
 * Markup for the git-update form. Uses WHMCS Blend/Bootstrap 3 classes so
 * the addon page (addonmodules.php) matches the rest of admin. Do not wrap
 * this in WHMCS\Admin from /modules/servers/rackflow/ — those templates
 * resolve CSS/JS relative to this directory and 404.
 *
 * @param string $actionUrl JSON POST endpoint
 * @param bool $embedded True when WHMCS already printed the page heading
 * @return string
 */
function rackflow_gitupdate_adminPageHtml($actionUrl, $embedded = true)
{
    $actionUrlJson = json_encode($actionUrl, JSON_HEX_TAG | JSON_HEX_AMP | JSON_HEX_APOS | JSON_HEX_QUOT);
    if ($actionUrlJson === false) {
        $actionUrlJson = '""';
    }
    $heading = $embedded ? '' : '<h1>RackFlow Git Updates</h1>';
    return <<<HTML
<div class="rf-gu">
  {$heading}
  <p>Download <code>whmcs/modules/servers/rackflow</code> from a GitHub or Forgejo/Gitea branch and replace the files on this WHMCS server. This is not WHMCS's own updater.</p>
  <div class="form-group">
    <label for="rf-gu-url">Git repository URL</label>
    <input id="rf-gu-url" class="form-control" type="text" autocomplete="off" placeholder="https://github.com/Kenzim/dcim"/>
    <span class="help-block">HTTPS clone URL, or SSH form <code>git@host:owner/repo.git</code> (converted to HTTP(S) for download).</span>
  </div>
  <div class="row">
    <div class="col-sm-4">
      <div class="form-group">
        <label for="rf-gu-branch">Branch</label>
        <input id="rf-gu-branch" class="form-control" type="text" autocomplete="off" placeholder="main"/>
      </div>
    </div>
    <div class="col-sm-8">
      <div class="form-group">
        <label for="rf-gu-token">Access token <span class="text-muted">(optional)</span></label>
        <input id="rf-gu-token" class="form-control" type="password" autocomplete="new-password" placeholder=""/>
        <span class="help-block">Leave blank to keep a saved token. Needed for private repos.</span>
      </div>
    </div>
  </div>
  <div class="checkbox"><label><input id="rf-gu-clear-token" type="checkbox"/> Clear saved token</label></div>
  <div class="checkbox"><label><input id="rf-gu-insecure" type="checkbox"/> Allow insecure TLS (LAN git with a self-signed certificate)</label></div>
  <div class="checkbox"><label><input id="rf-gu-reseller" type="checkbox" checked/> Also update <code>rackflow_reseller</code> if that module is installed</label></div>
  <div class="well" id="rf-gu-meta"></div>
  <p>
    <button type="button" class="btn btn-default" id="rf-gu-save">Save settings</button>
    <button type="button" class="btn btn-default" id="rf-gu-check">Check for updates</button>
    <button type="button" class="btn btn-primary" id="rf-gu-update">Update now</button>
  </p>
  <pre class="well" id="rf-gu-log">Loading…</pre>
</div>
<style>
.rf-gu { max-width: 920px; }
.rf-gu #rf-gu-meta, .rf-gu #rf-gu-log { white-space: pre-wrap; }
.rf-gu #rf-gu-log { background: #1b2430; color: #d7e2ea; min-height: 6em; max-height: 16em; overflow: auto; }
.rf-gu .checkbox { margin-top: 4px; margin-bottom: 4px; }
.rf-gu .btn[disabled] { opacity: .55; cursor: wait; }
</style>
<script>
(function () {
  var ACTION = {$actionUrlJson};
  var logEl = document.getElementById('rf-gu-log');
  var metaEl = document.getElementById('rf-gu-meta');

  function log(msg) {
    logEl.textContent = msg;
  }
  function setBusy(busy) {
    ['rf-gu-save','rf-gu-check','rf-gu-update'].forEach(function (id) {
      document.getElementById(id).disabled = !!busy;
    });
  }
  function payload() {
    return {
      repo_url: document.getElementById('rf-gu-url').value,
      branch: document.getElementById('rf-gu-branch').value,
      token: document.getElementById('rf-gu-token').value,
      clear_token: document.getElementById('rf-gu-clear-token').checked,
      insecure_tls: document.getElementById('rf-gu-insecure').checked,
      update_reseller: document.getElementById('rf-gu-reseller').checked
    };
  }
  function post(action, extra) {
    var body = payload();
    body.action = action;
    if (extra) {
      Object.keys(extra).forEach(function (k) { body[k] = extra[k]; });
    }
    return fetch(ACTION, {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/json',
        'X-Requested-With': 'XMLHttpRequest'
      },
      body: JSON.stringify(body)
    }).then(function (res) {
      return res.text().then(function (text) {
        var data = null;
        try {
          data = text ? JSON.parse(text) : null;
        } catch (e) {
          throw new Error(res.ok ? 'Updater returned invalid JSON.' : ('HTTP ' + res.status + ': ' + String(text).slice(0, 120)));
        }
        if (!data) throw new Error('HTTP ' + res.status);
        return data;
      });
    });
  }
  function fillStatus(data) {
    if (!data) return;
    if (data.repo_url) document.getElementById('rf-gu-url').value = data.repo_url;
    if (data.branch) document.getElementById('rf-gu-branch').value = data.branch;
    document.getElementById('rf-gu-insecure').checked = !!data.insecure_tls;
    document.getElementById('rf-gu-reseller').checked = data.update_reseller !== false;
    var lines = [];
    lines.push('Module path: ' + (data.module_dir || '') + (data.module_writable ? ' (writable)' : ' (NOT writable — chown to the PHP user)'));
    if (data.reseller_installed) {
      lines.push('Reseller module: installed' + (data.reseller_writable ? ', writable' : ' (not writable)'));
    } else {
      lines.push('Reseller module: not installed (skipped).');
    }
    lines.push('PHP zip: ' + (data.zip_available ? 'available' : 'MISSING'));
    if (data.token_set) lines.push('Access token: saved');
    if (data.last_sha) {
      lines.push('Last applied: ' + data.last_sha.slice(0, 12) + (data.last_at ? ' at ' + data.last_at : ''));
      if (data.last_message) lines.push(data.last_message);
    } else {
      lines.push('Last applied: never via this tool (a manual rsync does not record a sha).');
    }
    metaEl.textContent = lines.join('\\n');
  }
  function describeRemote(data) {
    if (!data || !data.ok) return data && data.error ? data.error : 'Check failed.';
    var sha = (data.sha || '').slice(0, 12);
    var msg = (data.message || '').split('\\n')[0];
    var state = data.up_to_date ? 'Already applied.' : 'Update available.';
    return state + ' ' + (data.repo || '') + '@' + (data.branch || '') + ' ' + sha + (msg ? ' — ' + msg : '');
  }

  document.getElementById('rf-gu-save').addEventListener('click', function () {
    setBusy(true);
    log('Saving…');
    post('save').then(function (data) {
      if (!data.ok) { log(data.error || 'Save failed.'); return; }
      fillStatus(data.status || {});
      log('Settings saved.');
    }).catch(function (err) { log(String(err && err.message ? err.message : err)); }).then(function () { setBusy(false); });
  });
  document.getElementById('rf-gu-check').addEventListener('click', function () {
    setBusy(true);
    log('Checking git…');
    post('check').then(function (data) {
      if (data.status) fillStatus(data.status);
      log(describeRemote(data));
    }).catch(function (err) { log(String(err && err.message ? err.message : err)); }).then(function () { setBusy(false); });
  });
  document.getElementById('rf-gu-update').addEventListener('click', function () {
    if (!window.confirm('Replace the RackFlow module files on this WHMCS server with the selected git branch? Uncommitted files in the module directory will be overwritten.')) {
      return;
    }
    setBusy(true);
    log('Downloading archive and updating…');
    post('update').then(function (data) {
      if (data.status) fillStatus(data.status);
      if (!data.ok) { log(data.error || 'Update failed.'); return; }
      var lines = ['Updated to ' + (data.sha || '').slice(0, 12)];
      if (data.message) lines.push(data.message.split('\\n')[0]);
      if (data.rackflow) {
        lines.push('RackFlow files copied: ' + data.rackflow.copied + ', removed: ' + data.rackflow.deleted);
        (data.rackflow.files || []).slice(0, 40).forEach(function (f) { lines.push('  ' + f); });
      }
      if (data.reseller && data.reseller.copied !== undefined) {
        lines.push('Reseller files copied: ' + data.reseller.copied + ', removed: ' + data.reseller.deleted);
      } else if (data.reseller && data.reseller.skipped) {
        lines.push(data.reseller.error || 'Reseller skipped.');
      }
      log(lines.join('\\n'));
    }).catch(function (err) { log(String(err && err.message ? err.message : err)); }).then(function () { setBusy(false); });
  });

  post('status').then(function (data) {
    if (!data.ok) { log(data.error || 'Could not load status.'); return; }
    fillStatus(data.status || data);
    log('Ready. Check for updates, then Update now.');
  }).catch(function (err) { log(String(err && err.message ? err.message : err)); });
})();
</script>
HTML;
}
