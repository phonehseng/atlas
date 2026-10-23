# Test helper: runs one wizard step through the setup page's API and prints its log.
#   drive.ps1 -Step jellyfin [-Values @{...}] [-Port 8599]
# Reads the test install's access key and admin password from test-install.json (test-only values).
param([Parameter(Mandatory)] [string] $Step, [hashtable] $Values = @{}, [int] $Port = 8599, [int] $TimeoutMin = 40, [switch] $Skip)
$ErrorActionPreference = 'Stop'
$t = Get-Content "$PSScriptRoot\test-install.json" -Raw | ConvertFrom-Json
$h = @{ 'X-Setup-Token' = $t.token }
$base = "http://127.0.0.1:$Port"
if ($Skip) {
    Invoke-RestMethod -Method Post -Headers $h -ContentType 'application/json' -Uri "$base/api/skip" -Body (@{ step = $Step } | ConvertTo-Json)
    "skipped $Step"; return
}
foreach ($k in @($Values.Keys)) { if ($Values[$k] -eq '{password}') { $Values[$k] = $t.password } }
$body = @{ step = $Step; values = $Values; password = $t.password } | ConvertTo-Json -Depth 4
try {
    $job = (Invoke-RestMethod -Method Post -Headers $h -ContentType 'application/json' -Uri "$base/api/run" -Body $body).job
} catch {
    "RUN REFUSED: $($_.ErrorDetails.Message)"; exit 2
}
$from = 0; $deadline = (Get-Date).AddMinutes($TimeoutMin); $lastProgress = ''
while ((Get-Date) -lt $deadline) {
    $r = Invoke-RestMethod -Headers $h -Uri "$base/api/job?id=$job&from=$from" -TimeoutSec 60
    foreach ($l in $r.lines) {
        $from = $l.n + 1
        if ($l.kind -eq 'progress') {
            if ($l.pct -ge 100 -or $l.text -ne $lastProgress -and ($l.n % 15 -eq 0)) { "  [{0,3}%] {1}" -f $l.pct, $l.text }
            $lastProgress = $l.text
            continue
        }
        "{0,-6} {1}" -f $l.kind, $l.text
    }
    if ($r.done) { break }
}
$state = Invoke-RestMethod -Headers $h -Uri "$base/api/state"
$s = $state.steps | Where-Object id -eq $Step
"== $Step -> $($s.status) $($s.message)"
