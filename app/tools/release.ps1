<#
Publishes a Marquee release on GitHub in one command: bumps the version, builds, writes the update manifest
(marquee-update.json, see docs\updates.md) and creates the GitHub release with the zip and the manifest.

    powershell -ExecutionPolicy Bypass -File tools\release.ps1 -Version 1.4.0 -Notes notes\1.4.0.md
    ... -Draft                      create it as a draft (Marquee only sees it once it's published)
    ... -DryRun                     do everything except git and gh; print those commands instead
    ... -InstallerDir ..\MediaStackSetup\dist [-InstallerVersion 1.4.0] [-InstallerNotes notes.md]
                                    also attach the Media Stack Setup builds and add the "installer" and
                                    "stack-assets" entries (version read from MediaStackSetup.exe --version)
    ... -Merge extra.json           merge more top-level manifest entries (e.g. "stack-assets") from a JSON file
    ... -ExtraAssets a.zip,b.zip    more files to upload with the release

Needs: gh (signed in with access to the repo), the .venv and everything build.ps1 needs.
If this folder is a git repository, the version bump is committed and tagged v<version> (author Phone) and pushed
before the release is created; otherwise gh creates the tag on the repo's default branch.
#>
param(
    [Parameter(Mandatory = $true)][string]$Version,
    [string]$Notes = '',
    [switch]$Draft,
    [switch]$DryRun,
    [string]$Repo = 'phonehseng/marquee',
    [string]$MinOs = '10.0.17763',   # Windows 10 1809: the oldest Windows with WebView2
    [switch]$Critical,
    [switch]$NoBuild,                 # use the build already in dist\ (its VERSION must match)
    [string]$InstallerDir = '',
    [string]$InstallerVersion = '',
    [string]$InstallerNotes = '',
    [string]$StackAssetsVersion = '',
    [string]$Merge = '',
    [string[]]$ExtraAssets = @()
)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$Version = $Version.TrimStart('v')
if ($Version -notmatch '^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$') { throw "Version must look like 1.4.0 or 1.4.0-beta.1 (got $Version)" }
$tag = "v$Version"
$prerelease = $Version.Contains('-')
$utf8 = New-Object System.Text.UTF8Encoding($false)
function Say($m) { Write-Host "== $m" -ForegroundColor Cyan }
function Run([string]$exe, [string[]]$arguments) {
    $shown = ($arguments | ForEach-Object { if ($_ -match '\s') { '"' + $_ + '"' } else { $_ } }) -join ' '
    if ($DryRun) { Write-Host "[dry run] $exe $shown" -ForegroundColor Yellow; return }
    & $exe @arguments
    if ($LASTEXITCODE) { throw "$exe failed (exit $LASTEXITCODE)" }
}
function Sha256([string]$path) { (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLower() }
function AssetUrl([string]$name) { "https://github.com/$Repo/releases/download/$tag/$name" }

if (-not $DryRun -and -not (Get-Command gh -ErrorAction SilentlyContinue)) { throw 'gh (GitHub CLI) is not installed' }
$notesText = if ($Notes) { (Get-Content -LiteralPath $Notes -Raw -Encoding UTF8).Trim() } else { "Marquee $Version" }

# 1. version
$init = Join-Path $root 'marquee\__init__.py'
$initText = [IO.File]::ReadAllText($init)
$oldVersion = [regex]::Match($initText, '__version__\s*=\s*"([^"]+)"').Groups[1].Value
if (-not $oldVersion) { throw "no __version__ in $init" }
Say "version $oldVersion -> $Version"
[IO.File]::WriteAllText($init, [regex]::Replace($initText, '__version__\s*=\s*"[^"]+"', "__version__ = `"$Version`""), $utf8)

try {
    # 2. build
    if (-not $NoBuild) {
        Say 'building'
        & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'build.ps1')
        if ($LASTEXITCODE) { throw 'build.ps1 failed' }
    }
    $zip = Join-Path $root 'dist\Marquee-windows-x64.zip'
    $built = (Get-Content -LiteralPath (Join-Path $root 'dist\Marquee\VERSION') -Raw).Trim()
    if ($built -ne $Version) { throw "dist\Marquee\VERSION says $built, not $Version" }
    if (-not (Test-Path $zip)) { throw "$zip is missing" }

    # 3. manifest
    Say 'writing the update manifest'
    $out = Join-Path $root "dist\release\$tag"
    New-Item -ItemType Directory -Force $out | Out-Null
    $short = if ($notesText.Length -gt 4000) { $notesText.Substring(0, 4000).TrimEnd() + "`n`n(more on GitHub)" } else { $notesText }
    $published = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    # One schema shared with Media Stack Setup (internal/core/manifest.go): see docs\updates.md
    $manifest = [ordered]@{
        schema    = 1
        published = $published
        marquee   = [ordered]@{
            version   = $Version
            notes     = $short
            published = $published
            notes_url = "https://github.com/$Repo/releases/tag/$tag"
            min_os    = $MinOs
            critical  = [bool]$Critical
            files     = [ordered]@{
                'windows-x64' = [ordered]@{ name = 'Marquee-windows-x64.zip'; url = (AssetUrl 'Marquee-windows-x64.zip')
                                            sha256 = (Sha256 $zip); size = (Get-Item $zip).Length }
            }
        }
    }
    $uploads = @($zip)
    if ($InstallerDir) {
        $files = Get-ChildItem -LiteralPath $InstallerDir -File | Where-Object { $_.Name -like 'MediaStackSetup*' -and $_.Extension -notin '.sha256', '.txt', '.json', '.md' }
        if (-not $files) { throw "no MediaStackSetup builds in $InstallerDir" }
        if (-not $InstallerVersion) {
            $exe = $files | Where-Object { $_.Extension -eq '.exe' } | Select-Object -First 1
            # the version in the installer's source next to its dist folder (a GUI exe prints nothing for --version)
            $src = Join-Path (Split-Path (Resolve-Path -LiteralPath $InstallerDir).Path -Parent) 'assets\assets.go'
            if (Test-Path -LiteralPath $src) { $InstallerVersion = [regex]::Match((Get-Content -LiteralPath $src -Raw), 'Version\s*=\s*"([^"]+)"').Groups[1].Value }
            if (-not $InstallerVersion -and $exe) { $InstallerVersion = [regex]::Match((& $exe.FullName --version | Out-String), '\d+\.\d+\.\d+[0-9A-Za-z.+-]*').Value }
        }
        if ($InstallerVersion -notmatch '^\d+\.\d+\.\d+') { throw 'say -InstallerVersion (the Media Stack Setup version)' }
        $InstallerVersion = $InstallerVersion.TrimStart('v')
        $ia = [ordered]@{}
        foreach ($f in $files) {
            $key = if ($f.Extension -eq '.exe') { 'windows-x64' } elseif ($f.Name -match 'arm64|aarch64') { 'linux-arm64' } elseif ($f.Name -match 'linux') { 'linux-x64' } else { $null }
            if (-not $key) { continue }
            $ia[$key] = [ordered]@{ name = $f.Name; url = (AssetUrl $f.Name); sha256 = (Sha256 $f.FullName); size = $f.Length }
            $uploads += $f.FullName
        }
        $inotes = if ($InstallerNotes) { (Get-Content -LiteralPath $InstallerNotes -Raw -Encoding UTF8).Trim() } else { "Media Stack Setup $InstallerVersion" }
        $manifest['installer'] = [ordered]@{ version = $InstallerVersion; notes = $inotes; files = $ia }
        # the MediaSorter / Media Hub / health check files are built into the installer: same version
        $sav = if ($StackAssetsVersion) { $StackAssetsVersion.TrimStart('v') } else { $InstallerVersion }
        $manifest['stack-assets'] = [ordered]@{ version = $sav; min_installer = $InstallerVersion; notes = $inotes }
    }
    if ($Merge) {
        $extra = Get-Content -LiteralPath $Merge -Raw | ConvertFrom-Json
        foreach ($p in $extra.PSObject.Properties) { $manifest[$p.Name] = $p.Value }
    }
    foreach ($a in $ExtraAssets) { if (-not (Test-Path -LiteralPath $a)) { throw "$a is missing" }; $uploads += (Resolve-Path -LiteralPath $a).Path }
    $manifestFile = Join-Path $out 'marquee-update.json'
    [IO.File]::WriteAllText($manifestFile, ($manifest | ConvertTo-Json -Depth 10), $utf8)
    $notesFile = Join-Path $out 'notes.md'
    [IO.File]::WriteAllText($notesFile, $notesText, $utf8)
    $uploads += $manifestFile
    Write-Host ("   {0}  {1:N1} MB  sha256 {2}" -f 'Marquee-windows-x64.zip', ((Get-Item $zip).Length / 1MB), $manifest.marquee.files.'windows-x64'.sha256)
    Write-Host "   $manifestFile"

    # 4. git (only when this folder is a repository)
    $isGit = $false
    if (Get-Command git -ErrorAction SilentlyContinue) {
        $ErrorActionPreference = 'Continue'   # git's "not a git repository" on stderr is an answer, not an error
        $null = & git -C $root rev-parse --is-inside-work-tree 2>&1
        $isGit = ($LASTEXITCODE -eq 0)
        $ErrorActionPreference = 'Stop'
    }
    if ($isGit) {
        Say 'committing and tagging'
        Run git @('-C', $root, 'add', 'marquee/__init__.py')
        Run git @('-C', $root, 'commit', '-m', "Marquee $Version")   # as the repo's configured git user
        Run git @('-C', $root, 'tag', '-a', $tag, '-m', "Marquee $Version")
        Run git @('-C', $root, 'push', 'origin', 'HEAD', $tag)
    } else {
        Say 'not a git repository: gh creates the tag on the repo'
    }

    # 5. the GitHub release
    Say "creating the release $tag on $Repo"
    $ghArgs = @('release', 'create', $tag) + $uploads + @('--repo', $Repo, '--title', "Marquee $Version", '--notes-file', $notesFile)
    if ($isGit) { $ghArgs += '--verify-tag' }
    if ($Draft) { $ghArgs += '--draft' }
    if ($prerelease) { $ghArgs += '--prerelease' } else { $ghArgs += '--latest' }
    Run gh $ghArgs
    if ($DryRun) { Say "dry run done: nothing was committed or uploaded; dist\ holds the $Version build" }
    else { Say "released: https://github.com/$Repo/releases/tag/$tag" }
}
finally {
    if ($DryRun) {
        # a dry run leaves the source at its old version
        [IO.File]::WriteAllText($init, $initText, $utf8)
    }
}
