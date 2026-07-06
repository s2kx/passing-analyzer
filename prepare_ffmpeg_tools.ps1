$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$cache = Join-Path $root ".cache\ffmpeg"
$zip = Join-Path $cache "ffmpeg-release-essentials.zip"
$extract = Join-Path $cache "extract"
$tools = Join-Path $root "tools"
$url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"

New-Item -ItemType Directory -Force -Path $cache | Out-Null

if (-not (Test-Path $zip)) {
    Write-Host "Downloading FFmpeg release essentials..."
    Invoke-WebRequest -Uri $url -OutFile $zip
}

if (Test-Path $extract) {
    Remove-Item -LiteralPath $extract -Recurse -Force
}

Write-Host "Extracting FFmpeg..."
Expand-Archive -LiteralPath $zip -DestinationPath $extract -Force

$bin = Get-ChildItem -LiteralPath $extract -Recurse -Directory -Filter "bin" | Select-Object -First 1
if (-not $bin) {
    throw "ffmpeg bin folder not found"
}

New-Item -ItemType Directory -Force -Path $tools | Out-Null
Copy-Item -LiteralPath (Join-Path $bin.FullName "ffmpeg.exe") -Destination (Join-Path $tools "ffmpeg.exe") -Force
Copy-Item -LiteralPath (Join-Path $bin.FullName "ffprobe.exe") -Destination (Join-Path $tools "ffprobe.exe") -Force

& (Join-Path $tools "ffmpeg.exe") -version | Select-Object -First 1
Write-Host "FFmpeg tools are ready: $tools"
