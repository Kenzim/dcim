Start-Transcript -Path C:\Windows\Temp\firstboot.log

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

Write-Host "[2/5] Configuring network adapters for DHCP..." -ForegroundColor Yellow

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
# 3. Enable RDP
# --------------------------------

Write-Host "[3/5] Enabling Remote Desktop (RDP)..." -ForegroundColor Yellow

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
# 4. Extend C: partition
# --------------------------------

Write-Host "[4/4] Extending C: partition to maximum size..." -ForegroundColor Yellow

try {
    $driveLetter = "C"
    $partition = Get-Partition -DriveLetter $driveLetter -ErrorAction Stop
    $diskNumber = $partition.DiskNumber
    
    Write-Host "  Current C: partition size: $([math]::Round($partition.Size / 1GB, 2)) GB" -ForegroundColor Gray
    
    $supported = Get-PartitionSupportedSize -DiskNumber $diskNumber -PartitionNumber $partition.PartitionNumber -ErrorAction Stop
    
    if ($partition.Size -ge $supported.SizeMax) {
        Write-Host "  C: is already at maximum size ($([math]::Round($supported.SizeMax / 1GB, 2)) GB)" -ForegroundColor Green
        Write-Host "  [SKIP] No extension needed" -ForegroundColor Yellow
    } else {
        Write-Host "  Extending to maximum size: $([math]::Round($supported.SizeMax / 1GB, 2)) GB" -ForegroundColor Gray
        Resize-Partition -DiskNumber $diskNumber -PartitionNumber $partition.PartitionNumber -Size $supported.SizeMax -ErrorAction Stop
        Write-Host "  [SUCCESS] Extended C: to $([math]::Round($supported.SizeMax / 1GB, 2)) GB" -ForegroundColor Green
    }
} catch {
    Write-Error "  [FAILED] Failed to extend C: partition: $_"
    Write-Host "  Error details: $($_.Exception.Message)" -ForegroundColor Red
}

Write-Host ""

# --------------------------------
# 5. Format additional data disks
# --------------------------------

Write-Host "[5/5] Formatting additional data disks..." -ForegroundColor Yellow

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
