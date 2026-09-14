Start-Transcript -Path C:\Windows\Temp\firstboot.log
$script:AdminPasswordPlain = $null

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "DCIM First Boot Configuration Script" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host ""

# -----------------------------
# 1. Set Administrator password
# -----------------------------

Write-Host "[1/4] Setting Administrator password..." -ForegroundColor Yellow

$pwFile = "C:\dcim_password.txt"

if (Test-Path $pwFile) {
    Write-Host "  Password file found at: $pwFile" -ForegroundColor Green
    
    try {
        # Read file content and trim whitespace/newlines
        # Files written from Linux may have different line endings
        $passwordPlain = Get-Content $pwFile -Raw -ErrorAction Stop
        $passwordPlain = $passwordPlain.Trim()
        
        if ([string]::IsNullOrWhiteSpace($passwordPlain)) {
            Write-Warning "  Password file is empty or contains only whitespace"
            Write-Host "  [SKIP] Administrator password not set (empty file)" -ForegroundColor Yellow
        } else {
            Write-Host "  Password file read successfully (length: $($passwordPlain.Length) characters)" -ForegroundColor Green
            
            # Log the password (masked) for verification
            $maskedPassword = if ($passwordPlain.Length -gt 4) {
                $passwordPlain.Substring(0, 2) + ("*" * ($passwordPlain.Length - 4)) + $passwordPlain.Substring($passwordPlain.Length - 2)
            } else {
                "*" * $passwordPlain.Length
            }
            Write-Host "  Password to set: $maskedPassword" -ForegroundColor Gray
            
            $script:AdminPasswordPlain = $passwordPlain
            $securePassword = ConvertTo-SecureString $passwordPlain -AsPlainText -Force

            # Set local Administrator password
            try {
                $admin = Get-LocalUser -Name "Administrator" -ErrorAction Stop
                Write-Host "  Administrator account found, setting password..." -ForegroundColor Gray
                
                $admin | Set-LocalUser -Password $securePassword -ErrorAction Stop
                
                Write-Host "  [SUCCESS] Administrator password set successfully" -ForegroundColor Green
                Write-Host "  Password was set to: $maskedPassword" -ForegroundColor Gray
                
            } catch {
                Write-Error "  [FAILED] Failed to set Administrator password: $_"
                Write-Host "  Error details: $($_.Exception.Message)" -ForegroundColor Red
            }

            # Delete password file (no need to overwrite, just remove)
            try {
                Remove-Item $pwFile -Force -ErrorAction Stop
                Write-Host "  Password file deleted successfully" -ForegroundColor Green
            } catch {
                Write-Warning "  Password file could not be deleted: $_"
                Write-Host "  WARNING: Password file still exists at $pwFile" -ForegroundColor Yellow
            }
        }
    } catch {
        Write-Error "  [FAILED] Failed to read password file: $_"
        Write-Host "  Error details: $($_.Exception.Message)" -ForegroundColor Red
    }
} else {
    Write-Warning "  Password file not found at: $pwFile"
    Write-Host "  [SKIP] Administrator password not set (file not found)" -ForegroundColor Yellow
    Write-Host "  This is normal if the password was not configured during installation" -ForegroundColor Gray
}

Write-Host ""

# --------------------------------
# 2. Force all adapters to DHCP
# --------------------------------

Write-Host "[2/6] Configuring network adapters for DHCP..." -ForegroundColor Yellow

$adapters = Get-NetAdapter | Where-Object { $_.Status -ne "Disabled" }

if ($adapters) {
    Write-Host "  Found $($adapters.Count) active network adapter(s)" -ForegroundColor Gray
    
    $adapters | ForEach-Object {
        try {
            Write-Host "  Configuring adapter: $($_.Name) (Index: $($_.InterfaceIndex))" -ForegroundColor Gray
            Set-NetIPInterface -InterfaceIndex $_.InterfaceIndex -Dhcp Enabled -AddressFamily IPv4 -ErrorAction Stop
            Set-DnsClientServerAddress -InterfaceIndex $_.InterfaceIndex -ResetServerAddresses -ErrorAction Stop
            Write-Host "    [SUCCESS] DHCP enabled on $($_.Name)" -ForegroundColor Green
        } catch {
            Write-Warning "    [FAILED] Failed to set DHCP on adapter $($_.Name): $_"
        }
    }
} else {
    Write-Host "  No active network adapters found" -ForegroundColor Yellow
}

Write-Host ""

# --------------------------------
# 3. Configure NTP (Cloudflare) and force sync
# --------------------------------

Write-Host "[3/6] Configuring Windows Time (Cloudflare NTP)..." -ForegroundColor Yellow

try {
    $ntpPeers = "time.cloudflare.com,0x8 1.1.1.1,0x8 1.0.0.1,0x8"
    Write-Host "  Setting NTP peers: $ntpPeers" -ForegroundColor Gray

    w32tm /config /syncfromflags:manual /manualpeerlist:"$ntpPeers" /update | Out-Null
    Write-Host "  Windows Time config updated" -ForegroundColor Green

    Set-Service -Name W32Time -StartupType Automatic -ErrorAction Stop
    Restart-Service -Name W32Time -Force -ErrorAction Stop
    Write-Host "  Windows Time service restarted" -ForegroundColor Green

    w32tm /resync /force | Out-Null
    Write-Host "  [SUCCESS] Time sync forced successfully" -ForegroundColor Green
} catch {
    Write-Warning "  [FAILED] Failed to configure/sync NTP: $_"
}

Write-Host ""

# --------------------------------
# 4. Enable RDP
# --------------------------------

Write-Host "[4/6] Enabling Remote Desktop (RDP)..." -ForegroundColor Yellow

try {
    # Enable RDP in registry
    Write-Host "  Configuring RDP registry settings..." -ForegroundColor Gray
    Set-ItemProperty `
        -Path "HKLM:\System\CurrentControlSet\Control\Terminal Server" `
        -Name "fDenyTSConnections" `
        -Value 0 `
        -ErrorAction Stop
    
    # Enable Network Level Authentication
    Set-ItemProperty `
        -Path "HKLM:\System\CurrentControlSet\Control\Terminal Server\WinStations\RDP-Tcp" `
        -Name "UserAuthentication" `
        -Value 1 `
        -ErrorAction Stop
    
    Write-Host "  Registry settings configured" -ForegroundColor Green
    
    # Enable firewall rules for RDP
    Write-Host "  Enabling RDP firewall rules..." -ForegroundColor Gray
    Enable-NetFirewallRule -DisplayGroup "Remote Desktop" -ErrorAction Stop | Out-Null
    Write-Host "  Firewall rules enabled" -ForegroundColor Green
    
    # Ensure TermService is running
    Write-Host "  Configuring Terminal Services..." -ForegroundColor Gray
    Set-Service -Name TermService -StartupType Automatic -ErrorAction Stop
    Start-Service -Name TermService -ErrorAction Stop
    Write-Host "  Terminal Services started" -ForegroundColor Green
    
    Write-Host "  [SUCCESS] Remote Desktop enabled successfully" -ForegroundColor Green
} catch {
    Write-Error "  [FAILED] Failed to enable RDP: $_"
    Write-Host "  Error details: $($_.Exception.Message)" -ForegroundColor Red
}

Write-Host ""

# --------------------------------
# 5. Extend C: partition (diskpart then PowerShell fallback)
# --------------------------------

Write-Host "[5/6] Extending C: partition to maximum size..." -ForegroundColor Yellow

# 4a. Use diskpart: select volume C, extend filesystem (and extend partition if free space exists)
$diskpartScript = @"
select volume C
extend
extend filesystem
exit
"@
$diskpartFile = Join-Path $env:TEMP "dcim_extend_c.diskpart"
try {
    Set-Content -Path $diskpartFile -Value $diskpartScript -Encoding ASCII -ErrorAction Stop
    Write-Host "  Running diskpart to extend C: (select volume C, extend, extend filesystem)..." -ForegroundColor Gray
    $diskpartOut = & diskpart /s $diskpartFile 2>&1
    $diskpartOut | ForEach-Object { Write-Host "  diskpart: $_" -ForegroundColor Gray }
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  [SUCCESS] diskpart extended C: volume" -ForegroundColor Green
    } else {
        Write-Warning "  diskpart exited with code $LASTEXITCODE; trying PowerShell fallback"
    }
} catch {
    Write-Warning "  diskpart failed: $_"
} finally {
    if (Test-Path $diskpartFile) { Remove-Item $diskpartFile -Force -ErrorAction SilentlyContinue }
}

# 4b. PowerShell fallback if diskpart did not extend (e.g. no contiguous space or already max)
try {
    $driveLetter = "C"
    $partition = Get-Partition -DriveLetter $driveLetter -ErrorAction Stop
    $diskNumber = $partition.DiskNumber

    $currentSizeGB   = [math]::Round($partition.Size / 1GB, 2)
    $supported       = Get-PartitionSupportedSize -DiskNumber $diskNumber -PartitionNumber $partition.PartitionNumber -ErrorAction Stop
    $supportedMaxGB  = [math]::Round($supported.SizeMax / 1GB, 2)
    $deltaBytes      = $supported.SizeMax - $partition.Size

    Write-Host "  Current C: size: $currentSizeGB GB" -ForegroundColor Gray
    Write-Host "  Max supported C: size: $supportedMaxGB GB" -ForegroundColor Gray

    if ($deltaBytes -lt 1MB) {
        Write-Host "  C: partition is already using all available space" -ForegroundColor Green
    } elseif ($partition.Size -ge $supported.SizeMax) {
        Write-Host "  C: partition is already at maximum size ($supportedMaxGB GB)" -ForegroundColor Green
    } else {
        $targetBytes = [math]::Floor(($supported.SizeMax - 1MB) / 1MB) * 1MB
        $targetGB    = [math]::Round($targetBytes / 1GB, 2)
        Write-Host "  Extending via PowerShell to: $targetGB GB..." -ForegroundColor Gray
        Resize-Partition -DiskNumber $diskNumber -PartitionNumber $partition.PartitionNumber -Size $targetBytes -ErrorAction Stop
        Write-Host "  [SUCCESS] Extended C: to approximately $targetGB GB" -ForegroundColor Green
    }

    # Partition can be full while NTFS is still the image size (~20 GB).
    $vol = Get-Volume -DriveLetter $driveLetter -ErrorAction Stop
    $partNow = Get-Partition -DriveLetter $driveLetter -ErrorAction Stop
    if ($vol.Size -lt ($partNow.Size - 8MB)) {
        Write-Host "  NTFS is smaller than the partition; running extend filesystem..." -ForegroundColor Gray
        $fsFile = Join-Path $env:TEMP "dcim_extend_fs.diskpart"
        Set-Content -Path $fsFile -Value "select volume C`r`nextend filesystem`r`nexit" -Encoding ASCII
        & diskpart /s $fsFile | ForEach-Object { Write-Host "  diskpart: $_" -ForegroundColor Gray }
        Remove-Item $fsFile -Force -ErrorAction SilentlyContinue
        $vol = Get-Volume -DriveLetter $driveLetter -ErrorAction SilentlyContinue
        Write-Host "  C: filesystem size now $([math]::Round($vol.Size / 1GB, 2)) GB" -ForegroundColor Green
    }
} catch {
    $msg = $_.Exception.Message
    if ($msg -like "*Size Not Supported*" -or $msg -like "*extent is less than the minimum of 1MB*") {
        Write-Warning "  [SKIP] C: partition cannot be extended further (no free space >= 1 MB)."
        Write-Host "  Details: $msg" -ForegroundColor Gray
    } else {
        Write-Error "  [FAILED] Failed to extend C: partition: $_"
        Write-Host "  Error details: $msg" -ForegroundColor Red
    }
}

Write-Host ""

# --------------------------------
# 5b. Persist diskpart + serial admin cmd for every startup
# --------------------------------

$scriptsDir = "C:\Windows\Setup\Scripts"
if (!(Test-Path $scriptsDir)) {
    New-Item -Path $scriptsDir -ItemType Directory -Force | Out-Null
}

$extendFile = Join-Path $scriptsDir "extend-c.diskpart"
Set-Content -Path $extendFile -Value @"
select volume C
extend
extend filesystem
exit
"@ -Encoding ASCII

$serialPs1 = Join-Path $scriptsDir "serial-admin-cmd.ps1"
Set-Content -Path $serialPs1 -Value @'
$log = "C:\Windows\Temp\serial-admin-cmd.log"
function L($m) { Add-Content $log ("{0} {1}" -f (Get-Date -Format o), $m) -ErrorAction SilentlyContinue }
L "ps getty boot"

$mtx = New-Object System.Threading.Mutex($false, "Global\DCIM-SerialAdminCmd")
if (-not $mtx.WaitOne(0)) { L "already running"; return }

$script:QuietEmptyUntil = [datetime]::MinValue

function SW($sp, [string]$s) {
  if ([string]::IsNullOrEmpty($s)) { return }
  $b = [Text.Encoding]::UTF8.GetBytes($s)
  $sp.BaseStream.Write($b, 0, $b.Length)
  $sp.BaseStream.Flush()
}

function Mark-HostWrite($sp) {
  Start-Sleep -Milliseconds 20
  try { $sp.DiscardInBuffer() } catch {}
  $script:QuietEmptyUntil = (Get-Date).AddSeconds(2)
}

function Get-Cwd {
  try { (Get-Location).Path } catch { "C:\" }
}

function Write-Prompt($sp) {
  SW $sp ("$(Get-Cwd)`r`nRF> ")
  Mark-HostWrite $sp
}

function Drain-Eol($sp) {
  $prev = $sp.ReadTimeout
  $sp.ReadTimeout = 30
  try {
    while ($true) {
      try { $b = $sp.ReadByte() } catch { break }
      if ($b -ne 10 -and $b -ne 13) { break }
    }
  } finally { $sp.ReadTimeout = $prev }
}

function Read-SerialLine($sp) {
  $sb = New-Object System.Text.StringBuilder
  $escState = 0
  while ($sp.IsOpen) {
    $b = -1
    try { $b = $sp.ReadByte() } catch [System.TimeoutException] { continue } catch { return $null }
    if ($b -lt 0) { continue }
    if ($escState -eq 1) {
      if ($b -eq 91) { $escState = 2; continue }
      $escState = 0
      continue
    }
    if ($escState -eq 2) { $escState = 0; continue }
    if ($b -eq 27) { $escState = 1; continue }
    if ($b -eq 10) { continue }
    if ($b -eq 13) {
      Drain-Eol $sp
      return $sb.ToString()
    }
    if ($b -eq 3) { return "" }
    if ($b -eq 21) { [void]$sb.Clear(); continue }
    if ($b -eq 8 -or $b -eq 127) {
      if ($sb.Length -gt 0) { [void]$sb.Remove($sb.Length - 1, 1) }
      continue
    }
    if ($b -ge 32 -and $b -le 126) { [void]$sb.Append([char]$b) }
  }
  return $null
}

function Write-CmdOutput($sp, [string]$path) {
  if (!(Test-Path $path)) { return }
  $bytes = [IO.File]::ReadAllBytes($path)
  if ($bytes.Length -eq 0) { return }
  $txt = [Text.Encoding]::Default.GetString($bytes)
  $txt = $txt -replace "`r`n", "`n" -replace "`r", "`n" -replace "`n", "`r`n"
  SW $sp $txt
  if (-not $txt.EndsWith("`n")) { SW $sp "`r`n" }
}

foreach ($name in @("COM2", "COM1", "COM3")) {
  try {
    $sp = New-Object System.IO.Ports.SerialPort $name, 115200, None, 8, One
    $sp.Handshake = "None"
    $sp.DtrEnable = $true
    $sp.RtsEnable = $true
    $sp.ReadTimeout = 50
    $sp.WriteTimeout = 2000
    $sp.NewLine = "`r`n"
    $sp.Open()
    L "opened $name"
    SW $sp "`r`nRackflow serial console - Administrator@$env:COMPUTERNAME`r`n"
    SW $sp "Microsoft Windows [Version $([Environment]::OSVersion.Version)]`r`n"
    Write-Prompt $sp
    while ($sp.IsOpen) {
      $line = Read-SerialLine $sp
      if ($null -eq $line) { break }
      $line = $line.Trim()
      if ($line -eq "") {
        if ((Get-Date) -lt $script:QuietEmptyUntil) { continue }
        Write-Prompt $sp
        continue
      }
      L "exec $line"
      $tmpo = "$env:TEMP\rf-ser.out"; $tmpe = "$env:TEMP\rf-ser.err"
      cmd.exe /c $line > $tmpo 2>$tmpe
      Write-CmdOutput $sp $tmpo
      Write-CmdOutput $sp $tmpe
      Write-Prompt $sp
    }
    try { $sp.Close() } catch {}
  } catch { L "$name fail $_" }
}
L "ps getty end"
'@ -Encoding UTF8

function Invoke-DcimSchtasks([string]$name, [string]$tr, [string]$extra) {
    schtasks /Delete /TN $name /F 2>$null | Out-Null
    for ($i = 1; $i -le 5; $i++) {
        cmd.exe /c "schtasks /Create /TN `"$name`" /SC ONSTART /RU SYSTEM /RL HIGHEST $extra /TR `"$tr`" /F"
        if ($LASTEXITCODE -eq 0) {
            Write-Host "  [SUCCESS] Startup task $name registered" -ForegroundColor Green
            return
        }
        Write-Warning "  schtasks $name attempt $i failed (exit $LASTEXITCODE); retrying"
        Start-Sleep -Seconds 3
    }
    Write-Warning "  Failed to create $name after retries"
    New-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run" -Name $name -Value $tr -PropertyType String -Force | Out-Null
}

Invoke-DcimSchtasks -name "DCIM-ExtendC" -tr "diskpart.exe /s $extendFile" -extra ""
Invoke-DcimSchtasks -name "DCIM-SerialAdminCmd" -tr "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File $serialPs1" -extra "/DELAY 0000:15"
try {
    $null = ([wmiclass]"Win32_Process").Create("powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$serialPs1`"")
    Write-Host "  Serial admin cmd started in background" -ForegroundColor Gray
} catch {
    Write-Warning "  Could not start serial admin cmd immediately: $_"
}

Write-Host "[5c] Configuring Administrator auto-logon and serial console..." -ForegroundColor Yellow

try {
    Set-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" -Name "DisableCAD" -Value 1 -Type DWord -ErrorAction Stop
    Write-Host "  Disabled Ctrl+Alt+Del requirement" -ForegroundColor Green
} catch {
    Write-Warning "  Failed to disable CAD: $_"
}

if ($script:AdminPasswordPlain) {
    try {
        $winlogon = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon"
        Set-ItemProperty -Path $winlogon -Name "AutoAdminLogon" -Value "1" -Type String -ErrorAction Stop
        Set-ItemProperty -Path $winlogon -Name "DefaultUserName" -Value "Administrator" -Type String -ErrorAction Stop
        Set-ItemProperty -Path $winlogon -Name "DefaultPassword" -Value $script:AdminPasswordPlain -Type String -ErrorAction Stop
        Set-ItemProperty -Path $winlogon -Name "DefaultDomainName" -Value $env:COMPUTERNAME -Type String -ErrorAction Stop
        Set-ItemProperty -Path $winlogon -Name "Shell" -Value "cmd.exe" -Type String -ErrorAction Stop
        Remove-ItemProperty -Path $winlogon -Name "AutoLogonCount" -ErrorAction SilentlyContinue
        Write-Host "  [SUCCESS] AutoAdminLogon enabled for Administrator (shell=cmd.exe)" -ForegroundColor Green
    } catch {
        Write-Warning "  Failed to enable AutoAdminLogon: $_"
    }
} else {
    Write-Warning "  Skipping AutoAdminLogon (no Administrator password available)"
}

try {
    Enable-PSRemoting -Force -SkipNetworkProfileCheck -ErrorAction Stop | Out-Null
    New-NetFirewallRule -DisplayName "RF-WinRM-5985" -Direction Inbound -Protocol TCP -LocalPort 5985 -Action Allow -ErrorAction SilentlyContinue | Out-Null
    Write-Host "  WinRM / WinRS enabled on 5985" -ForegroundColor Green
} catch {
    Write-Warning "  Failed to enable WinRM: $_"
}

Write-Host "  Serial Administrator cmd attaches to COM2 (then COM1/COM3) at 115200" -ForegroundColor Gray

Write-Host ""

# --------------------------------
# 6. Format additional data disks
# --------------------------------

Write-Host "[6/6] Formatting additional data disks..." -ForegroundColor Yellow

# Formats all non-OS, non-removable disks as NTFS and assigns drive letters
# Safe for templates: skips disk with C: and skips removable media

try {
    $osPartition = Get-Partition -DriveLetter C -ErrorAction Stop
    $osDiskNumber = $osPartition.DiskNumber
    
    $disks = Get-Disk |
        Where-Object {
            $_.Number -ne $osDiskNumber -and
            $_.BusType -ne 'USB' -and
            $_.BusType -ne 'SD' -and
            $_.IsSystem -eq $false
        }
    
    if ($disks) {
        Write-Host "  Found $($disks.Count) additional disk(s) to process" -ForegroundColor Gray
        
        foreach ($disk in $disks) {
            Write-Host "  Processing disk $($disk.Number) (Size: $([math]::Round($disk.Size / 1GB, 2)) GB)..." -ForegroundColor Gray
            
            # Initialize disk if RAW
            if ($disk.PartitionStyle -eq 'RAW') {
                Write-Host "    Initializing disk as GPT..." -ForegroundColor Gray
                Initialize-Disk -Number $disk.Number -PartitionStyle GPT -PassThru -ErrorAction Stop | Out-Null
            }
            
            # Skip if disk already has partitions with drive letters
            $existingPartitions = Get-Partition -DiskNumber $disk.Number |
                Where-Object { $_.DriveLetter }
            
            if ($existingPartitions) {
                Write-Host "    [SKIP] Disk $($disk.Number) already has formatted partitions" -ForegroundColor Yellow
                continue
            }
            
            # Create partition using full disk
            Write-Host "    Creating partition..." -ForegroundColor Gray
            $partition = New-Partition `
                -DiskNumber $disk.Number `
                -UseMaximumSize `
                -AssignDriveLetter `
                -ErrorAction Stop
            
            # Format NTFS
            Write-Host "    Formatting as NTFS..." -ForegroundColor Gray
            Format-Volume `
                -Partition $partition `
                -FileSystem NTFS `
                -NewFileSystemLabel "DATA$($disk.Number)" `
                -Confirm:$false `
                -Force `
                -ErrorAction Stop
            
            Write-Host "    [SUCCESS] Disk $($disk.Number) formatted and mounted as $($partition.DriveLetter):" -ForegroundColor Green
        }
        
        Write-Host "  [SUCCESS] All available data disks processed" -ForegroundColor Green
    } else {
        Write-Host "  No additional data disks found" -ForegroundColor Gray
    }
} catch {
    Write-Error "  [FAILED] Error processing data disks: $_"
    Write-Host "  Error details: $($_.Exception.Message)" -ForegroundColor Red
}



$taskName = "RunOnce-UserLoginScript"
$script   = "C:\Windows\Setup\Scripts\user-login.ps1"
$ps       = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"

if (!(Test-Path $script)) {
    Write-Warning "user-login.ps1 missing: $script"
} else {
    $taskCmd = "cmd.exe /c `"`"$ps`" -NoProfile -ExecutionPolicy Bypass -File `"$script`" && schtasks /Delete /TN `"$taskName`" /F`""

    schtasks /Delete /TN $taskName /F 2>$null | Out-Null

    schtasks /Create /TN $taskName /SC ONLOGON /RU SYSTEM /RL HIGHEST /TR $taskCmd /F
    $code = $LASTEXITCODE

    if ($code -ne 0) {
        Write-Error "FAILED to create scheduled task ($taskName). schtasks exit code: $code"
    } else {
        Write-Host "OK created scheduled task: $taskName"
        # Print the stored definition so you can see it in firstboot.log
        schtasks /Query /TN $taskName /V /FO LIST
    }
}


Stop-Transcript
