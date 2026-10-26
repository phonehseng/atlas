# Builds Marquee for Windows into dist\Marquee (Marquee.exe + its files) and dist\Marquee-windows-x64.zip.
#   powershell -ExecutionPolicy Bypass -File build.ps1
# Needs: the .venv with requirements.txt + pyinstaller, and libmpv-2.dll in vendor\mpv (tools\get_mpv.ps1).
# The media index and the assistant's voice/GPU parts are left out: the app fetches them as pinned wheels on first
# use (marquee\wheels.py). Those import only stdlib modules the app already bundles, except numpy.testing's
# unittest, added below; POSIX-only, tkinter and venv imports in them are never reached on Windows.
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$py = '.venv\Scripts\python.exe'
if (-not (Test-Path 'vendor\mpv\libmpv-2.dll')) { & "$PSScriptRoot\tools\get_mpv.ps1" }
if (-not (Test-Path 'assets\marquee.ico')) { & $py tools\make_icon.py }
Get-ChildItem marquee -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force
& $py -m PyInstaller --noconfirm --clean --onedir --windowed --name Marquee --icon assets\marquee.ico `
    --add-data "marquee\web;marquee\web" --add-data "marquee\osc;marquee\osc" --add-data "assets\marquee.png;assets" `
    --add-data "vendor\anime4k;vendor\anime4k" --add-data "vendor\rnnoise;vendor\rnnoise" --add-data "marquee\apply_update.ps1;marquee" `
    --add-binary "vendor\mpv\libmpv-2.dll;mpv" --collect-submodules webview --hidden-import clr `
    --hidden-import unittest `
    --exclude-module faster_whisper --exclude-module ctranslate2 --exclude-module onnxruntime `
    --exclude-module numpy --exclude-module tokenizers --exclude-module sounddevice --exclude-module webrtcvad `
    --exclude-module av --exclude-module nvidia --exclude-module huggingface_hub --exclude-module openwakeword `
    --workpath build --distpath dist run_marquee.py
if ($LASTEXITCODE) { throw 'PyInstaller failed' }
# libmpv is looked up next to Marquee.exe
Move-Item -Force dist\Marquee\_internal\mpv dist\Marquee\mpv
Copy-Item LICENSE, README.md dist\Marquee
# VERSION: what this build is (the updater checks a downloaded build against its release manifest)
$version = [regex]::Match((Get-Content marquee\__init__.py -Raw), '__version__\s*=\s*"([^"]+)"').Groups[1].Value
if (-not $version) { throw 'no __version__ in marquee\__init__.py' }
[IO.File]::WriteAllText("$PSScriptRoot\dist\Marquee\VERSION", $version)
$zip = 'dist\Marquee-windows-x64.zip'
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path dist\Marquee -DestinationPath $zip
"{0}  {1:N0} MB" -f $zip, ((Get-Item $zip).Length / 1MB)
