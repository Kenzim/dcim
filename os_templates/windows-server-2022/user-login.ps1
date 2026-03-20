
Start-Transcript -Path C:\Windows\Temp\user-login.log -Append

"Ran at $(Get-Date -Format o) after logon by $env:USERNAME" |
  Out-File C:\Windows\Temp\user-login-ran.txt -Append -Encoding utf8

try {
  $ntpPeers = "time.cloudflare.com,0x8 1.1.1.1,0x8 1.0.0.1,0x8"
  Write-Host "Configuring Cloudflare NTP peers: $ntpPeers"
  w32tm /config /syncfromflags:manual /manualpeerlist:"$ntpPeers" /update | Out-Null
  Set-Service -Name W32Time -StartupType Automatic -ErrorAction Stop
  Restart-Service -Name W32Time -Force -ErrorAction Stop
  w32tm /resync /force | Out-Null
  Write-Host "NTP configured and resync forced."
} catch {
  Write-Warning "Failed to configure/sync NTP: $_"
}

try {
  $syncTaskName = "DCIM-ForceTimeSync-Every2Min"
  $syncTaskCmd = 'cmd.exe /c w32tm /resync /force'
  Write-Host "Configuring recurring time sync task: $syncTaskName"

  schtasks /Delete /TN $syncTaskName /F 2>$null | Out-Null
  schtasks /Create /TN $syncTaskName /SC MINUTE /MO 2 /RU SYSTEM /RL HIGHEST /TR $syncTaskCmd /F | Out-Null

  if ($LASTEXITCODE -eq 0) {
    Write-Host "Recurring 2-minute time sync task created."
  } else {
    Write-Warning "Failed to create recurring time sync task (exit code: $LASTEXITCODE)"
  }
} catch {
  Write-Warning "Failed to configure recurring time sync task: $_"
}

Start-Process cmd.exe -ArgumentList "/c C:\Windows\Setup\Scripts\MAS_AIO.cmd /Z-Windows" -Wait

schtasks /Delete /TN "RunOnce-UserLoginScript" /F | Out-Null


netsh int ipv4 set dynamicport tcp start=1025 num=64511
netsh int ipv4 set dynamicport udp start=1025 num=64511
Set-MPPreference -DisableTamperProtection $true
Set-mppreference -DisableRealtimeMonitoring $true

Stop-Transcript