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
# 5. Extend C: partition, then the NTFS filesystem
# --------------------------------
# diskpart stops a script when a command fails. On the system volume, "extend"
# often errors with "The parameter is incorrect" after the partition has already
# grown, so "extend filesystem" in the same script never runs. A later boot then
# fails "extend" (no free space) and skips the filesystem again.

Write-Host "[5/6] Extending C: partition and filesystem..." -ForegroundColor Yellow

$scriptsDir = "C:\Windows\Setup\Scripts"
if (!(Test-Path $scriptsDir)) {
    New-Item -Path $scriptsDir -ItemType Directory -Force | Out-Null
}
$extendPs1 = Join-Path $scriptsDir "extend-c.ps1"
$extendSrc = @'
$ErrorActionPreference = "Continue"
$dcimTranscript = $false
try {
    Start-Transcript -Path "C:\Windows\Temp\extend-c.log" -Append -ErrorAction Stop
    $dcimTranscript = $true
} catch {}

function Invoke-DcimDiskpart([string]$body) {
    $path = Join-Path $env:TEMP ("dcim-diskpart-" + [guid]::NewGuid().ToString("n") + ".txt")
    [System.IO.File]::WriteAllText($path, $body)
    try {
        & diskpart.exe /s $path 2>&1
    } finally {
        Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
    }
}

function Get-DcimFilesystemBytes([string]$drive) {
    return (New-Object System.IO.DriveInfo($drive)).TotalSize
}

$letter = "C"
try {
    Write-Host "Extending partition ${letter}:"
    Invoke-DcimDiskpart "select volume $letter`r`nextend noerr`r`nexit`r`n" | ForEach-Object { Write-Host "  diskpart: $_" }

    try {
        $partition = Get-Partition -DriveLetter $letter -ErrorAction Stop
        $supported = Get-PartitionSupportedSize -DiskNumber $partition.DiskNumber -PartitionNumber $partition.PartitionNumber -ErrorAction Stop
        $deltaBytes = $supported.SizeMax - $partition.Size
        if ($deltaBytes -ge 1MB) {
            $targetBytes = [math]::Floor(($supported.SizeMax - 1MB) / 1MB) * 1MB
            Write-Host ("  Resize-Partition to {0:N2} GB" -f ($targetBytes / 1GB))
            Resize-Partition -DiskNumber $partition.DiskNumber -PartitionNumber $partition.PartitionNumber -Size $targetBytes -ErrorAction Stop
        } else {
            Write-Host "  Partition is already using the free space"
        }
    } catch {
        $msg = $_.Exception.Message
        if ($msg -like "*Size Not Supported*" -or $msg -like "*extent is less than the minimum of 1MB*") {
            Write-Host "  Partition is already at maximum size"
        } else {
            Write-Warning "  Partition extend fallback failed: $msg"
        }
    }

    Write-Host "  extend filesystem"
    Invoke-DcimDiskpart "rescan`r`nselect volume $letter`r`nextend filesystem`r`nexit`r`n" | ForEach-Object { Write-Host "  diskpart: $_" }

    $partition = Get-Partition -DriveLetter $letter -ErrorAction Stop
    $fsBytes = Get-DcimFilesystemBytes $letter
    Write-Host ("  Partition {0:N2} GB, filesystem {1:N2} GB" -f ($partition.Size / 1GB), ($fsBytes / 1GB))
    if ($fsBytes -lt ($partition.Size - 16MB)) {
        Write-Host "  NTFS is still smaller than the partition; FSCTL_EXTEND_VOLUME"
        if (-not ("DcimVolumeExtend" -as [type])) {
            Add-Type -TypeDefinition @"
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
public static class DcimVolumeExtend {
    const uint GenericRead = 0x80000000;
    const uint FileShareReadWrite = 0x3;
    const uint OpenExisting = 3;
    const uint FileAttributeNormal = 0x80;
    const uint FsctlExtendVolume = 0x900F0;
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern SafeFileHandle CreateFile(string name, uint access, uint share, IntPtr sec, uint disp, uint flags, IntPtr template);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool DeviceIoControl(SafeFileHandle handle, uint code, ref long input, uint inputSize, IntPtr output, uint outputSize, out uint returned, IntPtr overlapped);
    public static void Extend(string driveLetter, long totalSectors) {
        string path = "\\\\.\\" + driveLetter + ":";
        using (SafeFileHandle handle = CreateFile(path, GenericRead, FileShareReadWrite, IntPtr.Zero, OpenExisting, FileAttributeNormal, IntPtr.Zero)) {
            if (handle.IsInvalid)
                throw new Win32Exception(Marshal.GetLastWin32Error());
            uint returned;
            long sectors = totalSectors;
            if (!DeviceIoControl(handle, FsctlExtendVolume, ref sectors, 8, IntPtr.Zero, 0, out returned, IntPtr.Zero))
                throw new Win32Exception(Marshal.GetLastWin32Error());
        }
    }
}
"@
        }
        $sector = [int](Get-Disk -Number $partition.DiskNumber).LogicalSectorSize
        if ($sector -lt 512) { $sector = 512 }
        try {
            [DcimVolumeExtend]::Extend($letter, [long][math]::Floor($partition.Size / $sector))
        } catch {
            Write-Warning "  FSCTL_EXTEND_VOLUME failed: $($_.Exception.Message)"
            if ($sector -ne 512) {
                try {
                    [DcimVolumeExtend]::Extend($letter, [long][math]::Floor($partition.Size / 512))
                } catch {
                    Write-Warning "  FSCTL_EXTEND_VOLUME (512-byte sectors) failed: $($_.Exception.Message)"
                }
            }
        }
        $fsBytes = Get-DcimFilesystemBytes $letter
    }
    if ($fsBytes -lt ($partition.Size - 16MB)) {
        Write-Warning ("  C: filesystem is still {0:N2} GB; partition is {1:N2} GB" -f ($fsBytes / 1GB), ($partition.Size / 1GB))
    } else {
        Write-Host ("  [SUCCESS] C: filesystem is {0:N2} GB" -f ($fsBytes / 1GB))
    }
} catch {
    Write-Warning "Extend C: failed: $($_.Exception.Message)"
} finally {
    if ($dcimTranscript) {
        try { Stop-Transcript } catch {}
    }
}
'@
try {
    Set-Content -Path $extendPs1 -Value $extendSrc -Encoding ASCII -ErrorAction Stop
    & $extendPs1
} catch {
    Write-Warning "  Failed to extend C: $_"
}

Write-Host ""

# --------------------------------
# 5b. Persist serial admin cmd for every startup
# --------------------------------

$scriptsDir = "C:\Windows\Setup\Scripts"
if (!(Test-Path $scriptsDir)) {
    New-Item -Path $scriptsDir -ItemType Directory -Force | Out-Null
}

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

function Start-DcimTaskScheduler {
    # schtasks prints "The network address is invalid" when Schedule is not
    # accepting local RPC. During specialize it is stopped, and it exits
    # again unless Time Broker is already running.
    foreach ($name in @("TimeBrokerSvc", "SystemEventsBroker", "Schedule")) {
        $svc = Get-Service -Name $name -ErrorAction SilentlyContinue
        if (-not $svc) { continue }
        try {
            if ($svc.StartType -eq "Disabled") {
                Set-Service -Name $name -StartupType Manual
            }
            if ((Get-Service -Name $name).Status -ne "Running") {
                Start-Service -Name $name
            }
        } catch {
            Write-Warning "  Could not start ${name}: $($_.Exception.Message)"
        }
    }
}

function Split-DcimCommand([string]$command) {
    $command = $command.Trim()
    if ($command.StartsWith('"')) {
        $end = $command.IndexOf('"', 1)
        if ($end -gt 1) {
            return @{
                Execute = $command.Substring(1, $end - 1)
                Argument = $command.Substring($end + 1).Trim()
            }
        }
    }
    $space = $command.IndexOf(" ")
    if ($space -lt 0) {
        return @{ Execute = $command; Argument = "" }
    }
    return @{
        Execute = $command.Substring(0, $space)
        Argument = $command.Substring($space + 1).Trim()
    }
}

function Register-DcimTaskCom([string]$name, [string]$command, [string]$kind, [string]$delayIso) {
    $parts = Split-DcimCommand $command
    $svc = New-Object -ComObject "Schedule.Service"
    $svc.Connect()
    $folder = $svc.GetFolder("\")
    try { $folder.DeleteTask($name, 0) } catch {}
    $def = $svc.NewTask(0)
    $def.RegistrationInfo.Description = $name
    # SID, not "SYSTEM": account-name lookup during specialize fails with
    # "The network address is invalid" when this host has no resolvable name.
    $def.Principal.UserId = "S-1-5-18"
    $def.Principal.LogonType = 5
    $def.Principal.RunLevel = 1
    $def.Settings.Enabled = $true
    $def.Settings.DisallowStartIfOnBatteries = $false
    $def.Settings.StopIfGoingOnBatteries = $false
    $action = $def.Actions.Create(0)
    $action.Path = $parts.Execute
    if ($parts.Argument) { $action.Arguments = $parts.Argument }
    if ($kind -eq "logon") {
        $trigger = $def.Triggers.Create(9)
    } else {
        $trigger = $def.Triggers.Create(8)
        if ($delayIso) { $trigger.Delay = $delayIso }
    }
    $trigger.Enabled = $true
    $null = $folder.RegisterTaskDefinition($name, $def, 6, "SYSTEM", $null, 5)
}

function Convert-DcimSchtasksDelay([string]$extra) {
    if ($extra -match '/DELAY\s+(\d+):(\d+)') {
        $total = ([int]$Matches[1] * 60) + [int]$Matches[2]
        if ($total -gt 0) { return "PT${total}S" }
    }
    return ""
}

function Invoke-DcimSchtasks([string]$name, [string]$tr, [string]$extra, [string]$kind = "boot") {
    $delayIso = Convert-DcimSchtasksDelay $extra
    $delayArg = $null
    if ($extra -match '/DELAY\s+(\S+)') { $delayArg = $Matches[1] }
    for ($i = 1; $i -le 5; $i++) {
        Start-DcimTaskScheduler
        try {
            Register-DcimTaskCom -name $name -command $tr -kind $kind -delayIso $delayIso
            Write-Host "  [SUCCESS] Scheduled task $name registered" -ForegroundColor Green
            return $true
        } catch {
            Write-Warning "  task $name attempt $i failed: $($_.Exception.Message)"
        }
        $argList = @("/Create", "/F", "/S", "localhost", "/TN", $name, "/RU", "SYSTEM", "/RL", "HIGHEST", "/TR", $tr)
        if ($kind -eq "logon") {
            $argList += @("/SC", "ONLOGON")
        } else {
            $argList += @("/SC", "ONSTART")
            if ($delayArg) { $argList += @("/DELAY", $delayArg) }
        }
        & schtasks.exe @argList | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "  [SUCCESS] Scheduled task $name registered" -ForegroundColor Green
            return $true
        }
        Write-Warning "  schtasks $name attempt $i failed (exit $LASTEXITCODE); retrying"
        Start-Sleep -Seconds 3
    }
    Write-Warning "  Failed to create $name after retries"
    if ($kind -eq "boot") {
        New-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run" -Name $name -Value $tr -PropertyType String -Force | Out-Null
    }
    return $false
}

$null = Invoke-DcimSchtasks -name "DCIM-ExtendC" -tr "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File $extendPs1" -extra ""
$null = Invoke-DcimSchtasks -name "DCIM-SerialAdminCmd" -tr "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File $serialPs1" -extra "/DELAY 0000:15"
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
        # Keep Explorer as the shell: Winlogon\Shell is machine-wide, so cmd.exe there gave RDP sessions a black screen.
        Set-ItemProperty -Path $winlogon -Name "Shell" -Value "explorer.exe" -Type String -ErrorAction Stop
        # Open a command prompt on the KVM/console session only.
        $runKey = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run"
        Set-ItemProperty -Path $runKey -Name "DCIMConsoleCmd" -Value 'cmd.exe /c if /i "%SESSIONNAME%"=="Console" start "" cmd.exe' -Type String -ErrorAction Stop
        Remove-ItemProperty -Path $winlogon -Name "AutoLogonCount" -ErrorAction SilentlyContinue
        Write-Host "  [SUCCESS] AutoAdminLogon enabled for Administrator (console cmd prompt)" -ForegroundColor Green
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
    $created = Invoke-DcimSchtasks -name $taskName -tr $taskCmd -extra "" -kind "logon"
    if (-not $created) {
        Write-Error "FAILED to create scheduled task ($taskName)."
    } else {
        Write-Host "OK created scheduled task: $taskName"
        schtasks.exe /Query /TN $taskName /V /FO LIST
    }
}


Stop-Transcript
