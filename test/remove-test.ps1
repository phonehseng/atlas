# Test helper: removes the throwaway test install completely (tasks, processes, D:\StackTest).
# Never touches the real "Media Stack"/"Media Server" installs.
param([string] $Root = 'D:\StackTest', [string] $Folder = '\Media Stack Test\')
$ErrorActionPreference = 'Continue'
if ($Root -notmatch 'StackTest' -or $Folder -notmatch 'Test') { throw 'refusing: this only removes test installs' }
try { $t = Get-Content "$PSScriptRoot\test-install.json" -Raw | ConvertFrom-Json
      Invoke-RestMethod -Method Post -Headers @{ 'X-Setup-Token' = $t.token } -ContentType 'application/json' -Uri http://127.0.0.1:8599/api/quit -Body '{}' -TimeoutSec 5 | Out-Null } catch {}
Get-ScheduledTask -TaskPath $Folder -ErrorAction SilentlyContinue | ForEach-Object {
    Stop-ScheduledTask -TaskPath $Folder -TaskName $_.TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskPath $Folder -TaskName $_.TaskName -Confirm:$false
    "removed task $($_.TaskName)"
}
Unregister-ScheduledTask -TaskName 'MediaStackSetup-Test' -Confirm:$false -ErrorAction SilentlyContinue
# Anything still running from the test folder (qBittorrent runs from Program Files with a test profile).
Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -like "$Root\*" -or $_.CommandLine -like "*$Root*" } | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; "stopped $($_.Name) ($($_.ProcessId))"
}
$desk = [Environment]::GetFolderPath('Desktop')
Get-ChildItem $desk -Filter '*(Media Stack Test).url' -ErrorAction SilentlyContinue | ForEach-Object { Remove-Item $_.FullName; "removed shortcut $($_.Name)" }
Start-Sleep 3
Remove-Item -Recurse -Force $Root -ErrorAction SilentlyContinue
if (Test-Path $Root) { "some files in $Root are still locked - run again in a minute" } else { "removed $Root" }
