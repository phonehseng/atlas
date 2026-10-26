# Copies this working folder into the combined repo's app\ folder (everything except local-only files), so the
# repo always has the current Marquee source.  powershell -ExecutionPolicy Bypass -File tools\sync_to_repo.ps1
param([string]$Repo = 'D:\Videos\MediaStackSetup')
$src = Split-Path $PSScriptRoot -Parent
$dst = Join-Path $Repo 'app'
robocopy $src $dst /MIR /NFL /NDL /NJH /NP `
    /XD .venv .devhome build dist __pycache__ .git (Join-Path $src 'vendor\mpv') `
    /XF *.pyc *.spec *.spec.bak dev.ps1 INTEGRATION.md mpv-dev.7z | Select-Object -Last 4
if ($LASTEXITCODE -ge 8) { throw "robocopy failed ($LASTEXITCODE)" }
$global:LASTEXITCODE = 0
