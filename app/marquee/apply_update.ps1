# Atlas's updater (started by marquee/updater.py, from a copy in the updates folder so it never holds the install
# folder open). It swaps the install folder for the new build once Atlas has closed, starts the new build and
# waits for its heartbeat; if that never comes, it puts the old folder back and starts that instead.
#   powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File apply_update.ps1 -Job <updates>\job.json
# job.json: pid, install, staged, old, exe, from, to, heartbeat, result, log, wait_exit, wait_start (seconds)
param([Parameter(Mandatory = $true)][string]$Job)
$ErrorActionPreference = 'Stop'
$j = Get-Content -LiteralPath $Job -Raw | ConvertFrom-Json
$install = $j.install; $old = $j.old; $staged = $j.staged
$failed = "$install.failed"

function Log([string]$m) {
    try { Add-Content -LiteralPath $j.log -Value ('{0:yyyy-MM-dd HH:mm:ss}  {1}' -f (Get-Date), $m) -Encoding UTF8 } catch {}
}
function Result([string]$status, [string]$msg) {
    $r = [ordered]@{ status = $status; from = $j.from; to = $j.to; message = $msg; at = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() }
    try { $r | ConvertTo-Json | Set-Content -LiteralPath $j.result -Encoding UTF8 } catch { Log "couldn't write the result: $_" }
}
# Retry an action for a while: a folder can stay busy for a moment after a process exits (antivirus, indexer).
function Retry([scriptblock]$action, [int]$seconds) {
    $until = (Get-Date).AddSeconds($seconds)
    while ($true) {
        try { & $action; return $true }
        catch {
            if ((Get-Date) -gt $until) { Log "gave up: $($_.Exception.Message)"; return $false }
            Start-Sleep -Milliseconds 500
        }
    }
}
function Launch([string[]]$arguments) {
    # whichever exe the folder has: Atlas.exe, else the job's, else Marquee.exe (a build from before the rename,
    # e.g. after a roll back)
    $exe = @('Atlas.exe', $j.exe, 'Marquee.exe') | Where-Object { $_ } | ForEach-Object { Join-Path $install $_ } |
        Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if (-not $exe) { $exe = Join-Path $install $j.exe }
    Log "starting $exe $($arguments -join ' ')"
    return Start-Process -FilePath $exe -ArgumentList $arguments -WorkingDirectory $install -PassThru
}
function Heartbeat-Ok {
    if (-not (Test-Path -LiteralPath $j.heartbeat)) { return $false }
    try { return ((Get-Content -LiteralPath $j.heartbeat -Raw | ConvertFrom-Json).version -eq $j.to) } catch { return $false }
}
function Stop-From([string]$dir) {
    # anything still running out of a folder that is about to move (the new build that failed)
    Get-Process -ErrorAction SilentlyContinue | Where-Object {
        try { $_.Path -and $_.Path.StartsWith($dir + '\', [StringComparison]::OrdinalIgnoreCase) } catch { $false }
    } | ForEach-Object { try { $_.Kill(); $_.WaitForExit(10000) | Out-Null } catch {} }
}

Log "update $($j.from) -> $($j.to): waiting for Atlas (pid $($j.pid)) to close"
# tell Atlas this script really runs (it only closes once it sees this)
try { Set-Content -LiteralPath $j.ack -Value $PID -Encoding ASCII } catch {}

# 1. Atlas closes through its normal path; wait for it.
$p = Get-Process -Id $j.pid -ErrorAction SilentlyContinue
if ($p -and -not $p.WaitForExit([int]$j.wait_exit * 1000)) {
    Log 'Atlas did not close in time; nothing was changed'
    Result 'cancelled' "Atlas didn't close, so the update wasn't applied."
    exit 1
}

# 2. Swap the folders: <install> -> <install>.old, <install>.new -> <install>. Both renames stay on one drive.
if (Test-Path -LiteralPath $old) { Retry { Remove-Item -LiteralPath $old -Recurse -Force } 20 | Out-Null }
if (-not (Retry { [IO.Directory]::Move($install, $old) } 30)) {
    Result 'failed' "The Atlas folder was still in use, so the update wasn't applied."
    Launch @('--update-failed', $j.to) | Out-Null
    exit 1
}
if (-not (Retry { [IO.Directory]::Move($staged, $install) } 15)) {
    Retry { [IO.Directory]::Move($old, $install) } 30 | Out-Null
    Result 'failed' "The new version couldn't be moved into place, so Atlas stayed as it was."
    Launch @('--update-failed', $j.to) | Out-Null
    exit 1
}
Log 'folders swapped'

# 3. Start the new build and wait for its heartbeat.
Remove-Item -LiteralPath $j.heartbeat -Force -ErrorAction SilentlyContinue
$ok = $false
try {
    $new = Launch @('--updated', $j.from)
    $deadline = (Get-Date).AddSeconds([int]$j.wait_start)
    while ((Get-Date) -lt $deadline) {
        if (Heartbeat-Ok) { $ok = $true; break }
        if ($new.HasExited) {
            Start-Sleep -Seconds 1
            if (Heartbeat-Ok) { $ok = $true } else { Log "the new version exited (code $($new.ExitCode)) before it started" }
            break
        }
        Start-Sleep -Milliseconds 500
    }
} catch { Log "the new version couldn't be started: $($_.Exception.Message)" }

if ($ok) {
    Log "Atlas $($j.to) started"
    Result 'updated' ''
    Retry { if (Test-Path -LiteralPath $old) { Remove-Item -LiteralPath $old -Recurse -Force } } 60 | Out-Null
    exit 0
}

# 4. Roll back: put the old folder back and start it.
Log 'no heartbeat from the new version: rolling back'
Stop-From $install
if (-not (Retry { [IO.Directory]::Move($install, $failed) } 30)) {
    Result 'failed' "The new version didn't start and the old one couldn't be put back. Your old copy is in $old."
    exit 2
}
Retry { [IO.Directory]::Move($old, $install) } 30 | Out-Null
Result 'failed' "Atlas $($j.to) didn't start, so Atlas went back to $($j.from)."
Launch @('--update-failed', $j.to) | Out-Null
Retry { if (Test-Path -LiteralPath $failed) { Remove-Item -LiteralPath $failed -Recurse -Force } } 30 | Out-Null
Log 'rolled back'
exit 1
