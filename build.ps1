# Builds Media Stack Setup for Windows (x64) and Linux (x64 + ARM64) into .\dist.
#   powershell -ExecutionPolicy Bypass -File build.ps1
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$go = if (Test-Path 'D:\Videos\Tools\go\bin\go.exe') { 'D:\Videos\Tools\go\bin\go.exe' } else { 'go' }
$env:GOTELEMETRY = 'off'
$env:CGO_ENABLED = '0'
$env:GOFLAGS = '-trimpath'
New-Item -ItemType Directory -Force dist | Out-Null

# Python caches must not end up inside the program.
Get-ChildItem assets -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force

& $go vet ./...
if ($LASTEXITCODE) { throw 'go vet failed' }
& $go test ./...
if ($LASTEXITCODE) { throw 'tests failed' }

$targets = @(
    @{ os = 'windows'; arch = 'amd64'; out = 'dist\MediaStackSetup.exe'; flags = '-s -w -H windowsgui' },
    @{ os = 'linux';   arch = 'amd64'; out = 'dist\MediaStackSetup-linux-x64'; flags = '-s -w' },
    @{ os = 'linux';   arch = 'arm64'; out = 'dist\MediaStackSetup-linux-arm64'; flags = '-s -w' }
)
foreach ($t in $targets) {
    $env:GOOS = $t.os; $env:GOARCH = $t.arch
    & $go build -ldflags $t.flags -o $t.out .
    if ($LASTEXITCODE) { throw "build failed for $($t.os)/$($t.arch)" }
    $h = (Get-FileHash $t.out -Algorithm SHA256).Hash.ToLower()
    "{0,-34} {1,6:N1} MB  sha256 {2}" -f $t.out, ((Get-Item $t.out).Length / 1MB), $h
}
Remove-Item Env:GOOS, Env:GOARCH
