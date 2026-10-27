# Builds Atlas for Windows into dist\Atlas (Atlas.exe + its files) and dist\Atlas-windows-x64.zip.
#   powershell -ExecutionPolicy Bypass -File build.ps1 [-OutDir <folder>] [-NoLegacyExe]
# -OutDir builds into another folder than dist\ (PyInstaller's work files then go to <folder>\.work).
# The zip holds the files at its top level (no folder in it): Atlas's updater, Marquee 1.5.1's updater and Media
# Stack Setup all accept that.
# Transition from the old name: the build also carries Marquee.exe, a copy of Atlas.exe (a PyInstaller onedir exe
# runs under any name; it only needs _internal next to it). Installs of Marquee 1.5.1 and older check an update for
# Marquee.exe and restart Marquee.exe after updating. Leave it out with -NoLegacyExe after the transition release
# (docs\updates.md).
# Needs: the .venv with requirements.txt + pyinstaller, and libmpv-2.dll in vendor\mpv (tools\get_mpv.ps1).
# The media index and the assistant's voice/GPU parts are left out: the app fetches them as pinned wheels on first
# use (marquee\wheels.py). Those import only stdlib modules the app already bundles, except numpy.testing's
# unittest (and plistlib, for AirPlay), added below; POSIX-only, tkinter and venv imports in them are never reached on Windows.
# Trailers: yt-dlp (yt-dlp-ejs comes along through yt-dlp's own PyInstaller hook) and QuickJS (vendor\quickjs) are bundled.
# Casting: pychromecast + zeroconf are bundled; pyatv (AirPlay) and its large dependencies are left out the same way
# and fetched as pinned wheels the first time an AirPlay TV is picked (wheels.py group "airplay").
param([string]$OutDir = '', [switch]$NoLegacyExe)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$py = '.venv\Scripts\python.exe'
$dist = if ($OutDir) { [IO.Path]::GetFullPath($OutDir) } else { Join-Path $PSScriptRoot 'dist' }
$work = if ($OutDir) { Join-Path $dist '.work' } else { Join-Path $PSScriptRoot 'build' }
$app = Join-Path $dist 'Atlas'
if (-not (Test-Path 'vendor\mpv\libmpv-2.dll')) { & "$PSScriptRoot\tools\get_mpv.ps1" }
if (-not (Test-Path 'vendor\quickjs\qjs.exe')) { & "$PSScriptRoot\tools\get_quickjs.ps1" }
if (-not (Test-Path 'assets\marquee.ico')) { & $py tools\make_icon.py }
Get-ChildItem marquee -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force
& $py -m PyInstaller --noconfirm --clean --onedir --windowed --name Atlas --icon assets\marquee.ico `
    --add-data "marquee\web;marquee\web" --add-data "marquee\osc;marquee\osc" --add-data "assets\marquee.png;assets" `
    --add-data "vendor\anime4k;vendor\anime4k" --add-data "vendor\rnnoise;vendor\rnnoise" --add-data "marquee\apply_update.ps1;marquee" `
    --add-binary "vendor\mpv\libmpv-2.dll;mpv" --add-binary "vendor\quickjs\qjs.exe;quickjs" --add-data "vendor\quickjs\LICENSE;quickjs" --collect-submodules webview --hidden-import clr `
    --hidden-import unittest --hidden-import plistlib --collect-submodules pychromecast --collect-submodules zeroconf `
    --exclude-module faster_whisper --exclude-module ctranslate2 --exclude-module onnxruntime `
    --exclude-module numpy --exclude-module tokenizers --exclude-module sounddevice --exclude-module webrtcvad `
    --exclude-module av --exclude-module nvidia --exclude-module huggingface_hub --exclude-module openwakeword `
    --exclude-module pyatv --exclude-module cryptography --exclude-module Cryptodome --exclude-module pydantic --exclude-module pydantic_core `
    --exclude-module aiohttp --exclude-module miniaudio --exclude-module srptools --exclude-module tinytag `
    --workpath $work --distpath $dist run_marquee.py
if ($LASTEXITCODE) { throw 'PyInstaller failed' }
# libmpv is looked up next to the exe
Move-Item -Force "$app\_internal\mpv" "$app\mpv"
Copy-Item LICENSE, README.md $app
if (-not $NoLegacyExe) { Copy-Item "$app\Atlas.exe" "$app\Marquee.exe" }
# VERSION: what this build is (the updater checks a downloaded build against its release manifest)
$version = [regex]::Match((Get-Content marquee\__init__.py -Raw), '__version__\s*=\s*"([^"]+)"').Groups[1].Value
if (-not $version) { throw 'no __version__ in marquee\__init__.py' }
[IO.File]::WriteAllText("$app\VERSION", $version)
$zip = Join-Path $dist 'Atlas-windows-x64.zip'
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path "$app\*" -DestinationPath $zip
"{0}  {1:N0} MB" -f $zip, ((Get-Item $zip).Length / 1MB)
