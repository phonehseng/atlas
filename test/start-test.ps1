# Test helper: starts the setup wizard for a throwaway test install next to the real media server
# (D:\StackTest, every port +20000, Task Scheduler folder "Media Stack Test"). It runs as a one-shot
# scheduled task so it behaves exactly as if the user had double-clicked it.
#   -Env @{ MEDIASTACK_MARQUEE_ZIP = 'D:\...\Marquee-windows-x64.zip' }   environment for the installer
#   -Extra '--update'                                                    more command-line options
param([string] $Exe = "$PSScriptRoot\..\dist\MediaStackSetup.exe", [string] $Root = 'D:\StackTest', [int] $Port = 8599,
      [hashtable] $Env = @{}, [string] $Extra = '')
$ErrorActionPreference = 'Stop'
$fixture = "$PSScriptRoot\test-install.json"
if (-not (Test-Path $fixture)) {
    # Test-only values: an access key for the wizard API and the throwaway install's admin password.
    $chars = [char[]]'abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    $pw = -join (1..20 | ForEach-Object { $chars | Get-Random })
    $tok = -join (1..32 | ForEach-Object { '{0:x}' -f (Get-Random -Maximum 16) })
    @{ token = $tok; password = $pw; user = 'stacktest' } | ConvertTo-Json | Set-Content -Encoding utf8 $fixture
}
$t = Get-Content $fixture -Raw | ConvertFrom-Json
$exePath = (Resolve-Path $Exe).Path
$arg = "--root `"$Root`" --port-offset 20000 --name `"Media Stack Test`" --no-browser --port $Port --token $($t.token) $Extra"
if ($Env.Count -gt 0) {
    # Task Scheduler can't set environment variables: start the installer from a hidden PowerShell that does.
    $set = ($Env.GetEnumerator() | ForEach-Object { "`$env:$($_.Key) = '$($_.Value -replace "'", "''")'" }) -join '; '
    $cmd = "$set; Start-Process -FilePath '$exePath' -ArgumentList '$($arg -replace "'", "''")' -WorkingDirectory '$(Split-Path $exePath)'"
    $enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($cmd))
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -WindowStyle Hidden -EncodedCommand $enc"
} else {
    $action = New-ScheduledTaskAction -Execute $exePath -Argument $arg -WorkingDirectory (Split-Path $exePath)
}
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 8)
Register-ScheduledTask -TaskName 'MediaStackSetup-Test' -Action $action -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName 'MediaStackSetup-Test'
for ($i = 0; $i -lt 30; $i++) {
    try { Invoke-RestMethod -Headers @{ 'X-Setup-Token' = $t.token } "http://127.0.0.1:$Port/api/state" -TimeoutSec 3 | Out-Null; "wizard up on $Port"; return } catch { Start-Sleep 1 }
}
throw 'wizard did not start'
