# Downloads QuickJS-ng (MIT) into vendor\quickjs: the JavaScript runtime yt-dlp uses to solve YouTube's stream
# challenges for title-page trailers (marquee\trailers.py). Pinned to a release and checked against its SHA-256.
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot
$ver = 'v0.17.0'
$sha = '2aeabf0092c3262d6b2609824418f7dd7ed1f1df939f73b2b15645230cac0d77'
$dir = "$root\vendor\quickjs"
New-Item -ItemType Directory -Force $dir | Out-Null
Invoke-WebRequest "https://github.com/quickjs-ng/quickjs/releases/download/$ver/qjs-windows-x86_64.exe" -OutFile "$dir\qjs.exe" -UseBasicParsing
if ((Get-FileHash "$dir\qjs.exe" -Algorithm SHA256).Hash -ne $sha) { Remove-Item "$dir\qjs.exe"; throw 'qjs.exe: SHA-256 mismatch' }
Invoke-WebRequest "https://raw.githubusercontent.com/quickjs-ng/quickjs/$ver/LICENSE" -OutFile "$dir\LICENSE" -UseBasicParsing
"QuickJS-ng $ver"
