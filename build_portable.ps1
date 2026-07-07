param(
    [switch]$NoPause
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$dist = Join-Path $root "dist\OvertakingToolPortable"
$runtime = Join-Path $root "portable\python"

function Copy-ProjectFile([string]$Name) {
    Copy-Item -LiteralPath (Join-Path $root $Name) -Destination $dist -Force
}

try {
    if (-not (Test-Path (Join-Path $runtime "python.exe"))) {
        throw "Bundled Python was not found: portable\python\python.exe. Run run.bat once, or run prepare_portable_python.ps1."
    }

    Write-Host ""
    Write-Host "=== Building portable distribution ==="
    Write-Host "Output: $dist"
    Write-Host ""

    if (Test-Path $dist) {
        Remove-Item -LiteralPath $dist -Recurse -Force
    }

    New-Item -ItemType Directory -Force -Path $dist | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $dist "portable") | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $dist "tools") | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $dist "output") | Out-Null

    foreach ($file in @(
        "run.bat",
        "web_gui.py",
        "detect_overtaking.py",
        "lidar_pcap.py",
        "gpmf_sync.py",
        "edit_csv.py",
        "extract_telemetry.py",
        "final_export.py",
        "requirements.txt",
        "README.md",
        "PORTABLE_DISTRIBUTION.md"
    )) {
        Copy-ProjectFile $file
    }

    $model = Join-Path $root "yolov8n.pt"
    if (Test-Path $model) {
        Copy-Item -LiteralPath $model -Destination $dist -Force
    } else {
        Write-Warning "yolov8n.pt was not found. Run run.bat once before distribution."
    }

    Copy-Item -LiteralPath (Join-Path $root "web_ui") -Destination (Join-Path $dist "web_ui") -Recurse -Force
    Copy-Item -LiteralPath $runtime -Destination (Join-Path $dist "portable\python") -Recurse -Force

    $ffmpeg = Join-Path $root "tools\ffmpeg.exe"
    if (Test-Path $ffmpeg) {
        Copy-Item -LiteralPath $ffmpeg -Destination (Join-Path $dist "tools") -Force
        $ffprobe = Join-Path $root "tools\ffprobe.exe"
        if (Test-Path $ffprobe) {
            Copy-Item -LiteralPath $ffprobe -Destination (Join-Path $dist "tools") -Force
        }
    } else {
        Write-Warning "tools\ffmpeg.exe was not found. Clip extraction will be skipped unless ffmpeg is on PATH."
    }

    Write-Host ""
    Write-Host "[OK] Portable package created:"
    Write-Host "  $dist"
    Write-Host ""
    Write-Host "Start with:"
    Write-Host "  $dist\run.bat"
    Write-Host ""
} catch {
    Write-Error $_
    if (-not $NoPause) {
        Read-Host "Press Enter to exit"
    }
    exit 1
}

if (-not $NoPause) {
    Read-Host "Press Enter to exit"
}
