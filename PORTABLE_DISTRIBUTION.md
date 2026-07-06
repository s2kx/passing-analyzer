# Portable Distribution

This project uses the Python-bundled portable distribution style.

## Runtime Layout

```text
OvertakingToolPortable/
  run_gui.bat
  run.bat
  web_gui.py
  detect_overtaking.py
  extract_telemetry.py
  final_export.py
  edit_csv.py
  lidar_pcap.py
  gpmf_sync.py
  web_ui/
  yolov8n.pt
  tools/
    ffmpeg.exe        optional, used for clip extraction and NVDEC probing
  portable/
    python/
      python.exe
      pythonw.exe
      Lib/site-packages/
  output/
```

## Build Input

Before running `build_portable.bat`, prepare:

- `portable\python\python.exe`
- `portable\python\pythonw.exe`
- Python packages from `requirements.txt`
- `yolov8n.pt`
- Optional: `tools\ffmpeg.exe`

You can create `portable\python` on the build PC with:

```bat
prepare_portable_python.bat
```

By default this installs the normal PyPI PyTorch wheel, which may be CPU-only.
For a GPU bundle, rebuild with a PyTorch CUDA wheel index, for example:

```bat
powershell -NoProfile -ExecutionPolicy Bypass -File prepare_portable_python.ps1 -Force -TorchIndexUrl https://download.pytorch.org/whl/cu130
```

You can place FFmpeg tools under `tools\` with:

```bat
prepare_ffmpeg_tools.bat
```

The generated package excludes local analysis results such as `output/`, `out/`, and input videos.

## Build

```bat
build_portable.bat
```

The package is created at:

```text
dist\OvertakingToolPortable
```

## User Startup

Users should run:

```bat
run_gui.bat
```

No system Python install or `.venv` setup is required on the user PC.
