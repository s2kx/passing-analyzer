param(
    [string]$PythonVersion = "3.13.14",
    [string]$TorchIndexUrl = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$runtime = Join-Path $root "portable\python"
$cache = Join-Path $root ".cache\portable-python"
$pythonZip = Join-Path $cache "python-$PythonVersion-embed-amd64.zip"
$getPip = Join-Path $cache "get-pip.py"
$pythonUrl = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"
$getPipUrl = "https://bootstrap.pypa.io/get-pip.py"
$env:YOLO_CONFIG_DIR = Join-Path $root ".ultralytics"
New-Item -ItemType Directory -Force -Path $env:YOLO_CONFIG_DIR | Out-Null

function Invoke-Checked {
    param(
        [string]$FilePath,
        [string[]]$Arguments
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $FilePath $($Arguments -join ' ')"
    }
}

if ((Test-Path $runtime) -and -not $Force) {
    throw "portable\python already exists. Re-run with -Force to recreate it."
}

if ($Force -and (Test-Path $runtime)) {
    Remove-Item -LiteralPath $runtime -Recurse -Force
}

New-Item -ItemType Directory -Force -Path $cache | Out-Null
New-Item -ItemType Directory -Force -Path $runtime | Out-Null

if (-not (Test-Path $pythonZip)) {
    Write-Host "Downloading Python $PythonVersion embeddable package..."
    Invoke-WebRequest -Uri $pythonUrl -OutFile $pythonZip
}

Write-Host "Extracting Python runtime..."
Expand-Archive -LiteralPath $pythonZip -DestinationPath $runtime -Force

$pth = Get-ChildItem -LiteralPath $runtime -Filter "python*._pth" | Select-Object -First 1
if (-not $pth) {
    throw "Could not find python ._pth file in $runtime"
}

$pthText = @(Get-Content -LiteralPath $pth.FullName)
if ($pthText -notcontains "..\..") {
    $dotIndex = [Array]::IndexOf($pthText, ".")
    if ($dotIndex -ge 0) {
        $before = $pthText[0..$dotIndex]
        $after = if ($dotIndex + 1 -lt $pthText.Count) { $pthText[($dotIndex + 1)..($pthText.Count - 1)] } else { @() }
        $pthText = @($before + "..\.." + $after)
    } else {
        $pthText = @($pthText + "..\..")
    }
}
$pthText = $pthText | ForEach-Object {
    if ($_ -eq "#import site") { "import site" } else { $_ }
}
Set-Content -LiteralPath $pth.FullName -Value $pthText -Encoding ASCII

if (-not (Test-Path $getPip)) {
    Write-Host "Downloading get-pip.py..."
    Invoke-WebRequest -Uri $getPipUrl -OutFile $getPip
}

$python = Join-Path $runtime "python.exe"
Write-Host "Installing pip..."
Invoke-Checked $python @($getPip, "--no-warn-script-location")

Write-Host "Installing project requirements..."
Invoke-Checked $python @("-m", "pip", "install", "--upgrade", "pip", "--no-warn-script-location")
Invoke-Checked $python @("-m", "pip", "install", "-r", (Join-Path $root "requirements.txt"), "--no-warn-script-location")

if ($TorchIndexUrl) {
    Write-Host "Installing PyTorch from custom index: $TorchIndexUrl"
    Invoke-Checked $python @("-m", "pip", "install", "--upgrade", "torch", "torchvision", "--index-url", $TorchIndexUrl, "--no-warn-script-location")
}

Write-Host "Verifying runtime imports..."
Invoke-Checked $python @("-c", "import sys, cv2, numpy, torch, ultralytics, webview; print('Python:', sys.version.split()[0]); print('OpenCV:', cv2.__version__); print('Ultralytics:', ultralytics.__version__); print('PyTorch:', torch.__version__); print('CUDA:', torch.cuda.is_available())")

Write-Host "Portable Python is ready: $runtime"
