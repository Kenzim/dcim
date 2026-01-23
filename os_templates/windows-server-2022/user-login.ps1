
Start-Transcript -Path C:\Windows\Temp\user-login.log -Append

"Ran at $(Get-Date -Format o) after logon by $env:USERNAME" |
  Out-File C:\Windows\Temp\user-login-ran.txt -Append -Encoding utf8

Start-Process cmd.exe -ArgumentList "/c C:\Windows\Setup\Scripts\MAS_AIO.cmd /Z-Windows" -Wait

schtasks /Delete /TN "RunOnce-UserLoginScript" /F | Out-Null

Stop-Transcript