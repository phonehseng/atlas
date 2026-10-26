# Downloads libmpv (shinchiro's Windows build, x86-64-v3) into vendor\mpv. Needs 7-Zip or Windows' tar.
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot
$rel = Invoke-RestMethod 'https://api.github.com/repos/shinchiro/mpv-winbuild-cmake/releases/latest'
$asset = $rel.assets | Where-Object { $_.name -match '^mpv-dev-x86_64-v3-.*\.7z$' } | Select-Object -First 1
New-Item -ItemType Directory -Force "$root\vendor\mpv" | Out-Null
$archive = "$root\vendor\mpv-dev.7z"
Invoke-WebRequest $asset.browser_download_url -OutFile $archive -UseBasicParsing
$7z = @('C:\Program Files\7-Zip\7z.exe', 'C:\Program Files (x86)\7-Zip\7z.exe') | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($7z) { & $7z x $archive "-o$root\vendor\mpv" -y | Out-Null } else { tar -xf $archive -C "$root\vendor\mpv" }
"libmpv: $($asset.name)"
