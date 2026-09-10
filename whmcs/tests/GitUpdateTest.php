<?php

use PHPUnit\Framework\TestCase;

final class GitUpdateTest extends TestCase
{
    private $originalDocumentRoot;

    protected function setUp(): void
    {
        $this->originalDocumentRoot = $_SERVER['DOCUMENT_ROOT'] ?? null;
    }

    protected function tearDown(): void
    {
        if ($this->originalDocumentRoot === null) {
            unset($_SERVER['DOCUMENT_ROOT']);
        } else {
            $_SERVER['DOCUMENT_ROOT'] = $this->originalDocumentRoot;
        }
    }

    private function whmcsRoot(): string
    {
        return realpath(__DIR__ . '/../');
    }

    public function testPageUrlAtDocumentRoot(): void
    {
        $_SERVER['DOCUMENT_ROOT'] = $this->whmcsRoot();
        $this->assertSame(
            '/modules/servers/rackflow/module_update.php',
            rackflow_gitupdatePageUrl()
        );
        $this->assertSame(
            '/modules/servers/rackflow/module_update_action.php',
            rackflow_gitupdateActionUrl()
        );
        $this->assertSame(
            '/modules/servers/rackflow/module_update.php',
            rackflow_gitupdateAdminEntryUrl()
        );
    }

    public function testPageUrlWhenWhmcsInSubdirectory(): void
    {
        $_SERVER['DOCUMENT_ROOT'] = dirname($this->whmcsRoot());
        $expectedPrefix = '/' . basename($this->whmcsRoot());
        $this->assertSame(
            $expectedPrefix . '/modules/servers/rackflow/module_update.php',
            rackflow_gitupdatePageUrl()
        );
    }

    public function testParseGithubHttpsUrl(): void
    {
        $parsed = rackflow_gitupdate_parseRepoUrl('https://github.com/Kenzim/dcim.git');
        $this->assertTrue($parsed['ok']);
        $this->assertSame('github', $parsed['provider']);
        $this->assertSame('Kenzim', $parsed['owner']);
        $this->assertSame('dcim', $parsed['repo']);
        $this->assertSame(
            'https://api.github.com/repos/Kenzim/dcim/branches/main',
            rackflow_gitupdate_branchApiUrl($parsed, 'main')
        );
        $this->assertSame(
            'https://api.github.com/repos/Kenzim/dcim/zipball/main',
            rackflow_gitupdate_archiveUrl($parsed, 'main')
        );
    }

    public function testParseForgejoSshUrl(): void
    {
        $parsed = rackflow_gitupdate_parseRepoUrl('ssh://git@192.168.11.92/kenzim/dcim.git');
        $this->assertTrue($parsed['ok']);
        $this->assertSame('gitea', $parsed['provider']);
        $this->assertSame('http://192.168.11.92', $parsed['origin']);
        $this->assertSame('kenzim', $parsed['owner']);
        $this->assertSame('dcim', $parsed['repo']);
        $this->assertSame(
            'http://192.168.11.92/api/v1/repos/kenzim/dcim/archive/main.zip',
            rackflow_gitupdate_archiveUrl($parsed, '')
        );
    }

    public function testParseRejectsEmptyAndTraversal(): void
    {
        $empty = rackflow_gitupdate_parseRepoUrl('');
        $this->assertFalse($empty['ok']);
        $bad = rackflow_gitupdate_parseRepoUrl('https://github.com/../dcim');
        $this->assertFalse($bad['ok']);
    }

    public function testZipNameSafety(): void
    {
        $this->assertTrue(rackflow_gitupdate_isSafeZipName('Kenzim-dcim-abc/whmcs/modules/servers/rackflow/rackflow.php'));
        $this->assertFalse(rackflow_gitupdate_isSafeZipName('../rackflow.php'));
        $this->assertFalse(rackflow_gitupdate_isSafeZipName('/tmp/x.php'));
        $this->assertFalse(rackflow_gitupdate_isSafeZipName("whmcs/modules/\0evil"));
    }

    public function testFindExtractedModuleDirPrefersWhmcsPath(): void
    {
        $root = sys_get_temp_dir() . '/rf-gitupdate-find-' . uniqid();
        $module = $root . '/Kenzim-dcim-abc/whmcs/modules/servers/rackflow';
        mkdir($module, 0777, true);
        file_put_contents($module . '/rackflow.php', "<?php\n");
        file_put_contents($module . '/hooks.php', "<?php\n");
        $this->assertSame($module, rackflow_gitupdate_findExtractedModuleDir($root));

        $reseller = dirname($module) . '/rackflow_reseller';
        mkdir($reseller, 0777, true);
        file_put_contents($reseller . '/rackflow_reseller.php', "<?php\n");
        $this->assertSame($reseller, rackflow_gitupdate_findExtractedResellerDir($module));
        $this->rmTree($root);
    }

    public function testSyncDirectoryCopiesUpdatesAndDeletes(): void
    {
        $base = sys_get_temp_dir() . '/rf-gitupdate-sync-' . uniqid();
        $src = $base . '/src';
        $dst = $base . '/dst';
        mkdir($src . '/sub', 0777, true);
        mkdir($dst . '/sub', 0777, true);
        file_put_contents($src . '/rackflow.php', "new\n");
        file_put_contents($src . '/sub/keep.txt', "keep\n");
        file_put_contents($dst . '/rackflow.php', "old\n");
        file_put_contents($dst . '/gone.txt', "bye\n");
        file_put_contents($dst . '/sub/keep.txt', "keep\n");

        $result = rackflow_gitupdate_syncDirectory($src, $dst);
        $this->assertTrue($result['ok'], (string)$result['error']);
        $this->assertSame(1, $result['copied']);
        $this->assertSame(1, $result['deleted']);
        $this->assertSame("new\n", file_get_contents($dst . '/rackflow.php'));
        $this->assertFalse(is_file($dst . '/gone.txt'));
        $this->assertSame("keep\n", file_get_contents($dst . '/sub/keep.txt'));
        $this->rmTree($base);
    }

    public function testValidateIncomingModuleRejectsArchiveWithoutUpdater(): void
    {
        $dir = sys_get_temp_dir() . '/rf-gitupdate-missing-' . uniqid();
        mkdir($dir, 0777, true);
        file_put_contents($dir . '/rackflow.php', "<?php\n");
        file_put_contents($dir . '/hooks.php', "<?php\n");
        $result = rackflow_gitupdate_validateIncomingModule($dir);
        $this->assertFalse($result['ok']);
        $this->assertStringContainsString('git_update.php', (string)$result['error']);
        $this->rmTree($dir);
    }

    public function testValidateIncomingModuleAcceptsCompleteTree(): void
    {
        $dir = sys_get_temp_dir() . '/rf-gitupdate-complete-' . uniqid();
        mkdir($dir, 0777, true);
        foreach (rackflow_gitupdate_requiredModuleFiles() as $rel) {
            file_put_contents($dir . '/' . $rel, "<?php\n");
        }
        $result = rackflow_gitupdate_validateIncomingModule($dir);
        $this->assertTrue($result['ok'], (string)$result['error']);
        $this->rmTree($dir);
    }

    public function testNormalizeGithubBranchPayload(): void
    {
        $norm = rackflow_gitupdate_normalizeBranchPayload(array(
            'name' => 'main',
            'commit' => array(
                'sha' => 'd49c3e1094c1deadbeef',
                'commit' => array(
                    'message' => "Share one BMC KVM session\n\nbody",
                    'author' => array('date' => '2026-09-10T12:00:00Z'),
                ),
            ),
        ), 'github');
        $this->assertTrue($norm['ok']);
        $this->assertSame('d49c3e1094c1deadbeef', $norm['sha']);
        $this->assertSame("Share one BMC KVM session\n\nbody", $norm['message']);
    }

    private function rmTree($dir): void
    {
        if (!is_dir($dir)) {
            return;
        }
        $it = new RecursiveIteratorIterator(
            new RecursiveDirectoryIterator($dir, FilesystemIterator::SKIP_DOTS),
            RecursiveIteratorIterator::CHILD_FIRST
        );
        foreach ($it as $item) {
            if ($item->isDir()) {
                rmdir($item->getPathname());
            } else {
                unlink($item->getPathname());
            }
        }
        rmdir($dir);
    }
}
