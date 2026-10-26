# Test helper: removes the throwaway test install completely (tasks, processes, shortcuts, D:\StackTest,
# which also holds the test Marquee's settings). Never touches the real "Media Stack"/"Media Server" installs.
param([string] $Root = 'D:\StackTest', [string] $Folder = '\Media Stack Test\', [string] $Name = 'Media Stack Test')
$ErrorActionPreference = 'Continue'
if ($Root -notmatch 'StackTest' -or $Folder -notmatch 'Test' -or $Name -notmatch 'Test') { throw 'refusing: this only removes test installs' }
try { $t = Get-Content "$PSScriptRoot\test-install.json" -Raw | ConvertFrom-Json
      Invoke-RestMethod -Method Post -Headers @{ 'X-Setup-Token' = $t.token } -ContentType 'application/json' -Uri http://127.0.0.1:8599/api/quit -Body '{}' -TimeoutSec 5 | Out-Null } catch {}
Get-ScheduledTask -TaskPath $Folder -ErrorAction SilentlyContinue | ForEach-Object {
    Stop-ScheduledTask -TaskPath $Folder -TaskName $_.TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskPath $Folder -TaskName $_.TaskName -Confirm:$false
    "removed task $($_.TaskName)"
}
Unregister-ScheduledTask -TaskName 'MediaStackSetup-Test' -Confirm:$false -ErrorAction SilentlyContinue
# Anything still running from the test folder (qBittorrent runs from Program Files with a test profile; the
# test Marquee and the installer's test copies run from inside it).
Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -like "$Root\*" -or $_.CommandLine -like "*$Root*" } | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; "stopped $($_.Name) ($($_.ProcessId))"
}
$desk = [Environment]::GetFolderPath('Desktop')
$programs = [Environment]::GetFolderPath('Programs')
foreach ($dir in $desk, $programs) {
    Get-ChildItem $dir -ErrorAction SilentlyContinue | Where-Object { $_.Name -like "*($Name).url" -or $_.Name -like "*($Name).lnk" } |
        ForEach-Object { Remove-Item $_.FullName; "removed shortcut $($_.FullName)" }
}
# Firewall rules made by the test install (deleting them needs an administrator).
$rules = @(Get-NetFirewallRule -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -like "*$Name*" })
# ...and the ones Windows adds when a test app's "allow on networks?" prompt is answered.
$rules += @(Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue | Where-Object { $_.Program -like "$Root\*" } |
    Get-NetFirewallRule -ErrorAction SilentlyContinue)
foreach ($r in $rules) {
    try { $r | Remove-NetFirewallRule -ErrorAction Stop; "removed firewall rule $($r.DisplayName)" }
    catch { "FIREWALL RULE LEFT (needs an administrator): $($r.DisplayName)" }
}
Start-Sleep 3
Remove-Item -Recurse -Force $Root -ErrorAction SilentlyContinue
if (Test-Path $Root) { "some files in $Root are still locked - run again in a minute" } else { "removed $Root" }
