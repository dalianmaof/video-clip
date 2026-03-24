param(
    [string]$BuildDir = "build",
    [string]$DistDir = "dist",
    [string]$Name = "VideoBatchProcessor"
)

$ErrorActionPreference = "Stop"

Write-Host "Installing PyInstaller if needed..."
python -m pip install --upgrade pyinstaller --break-system-packages | Out-Null

Write-Host "Cleaning old build artifacts..."
Remove-Item -Recurse -Force $BuildDir, $DistDir -ErrorAction SilentlyContinue

Write-Host "Building onedir package..."
python -m PyInstaller `
    --noconfirm `
    --clean `
    --onedir `
    --workpath $BuildDir `
    --distpath $DistDir `
    --name $Name `
    process_videos.py

$appDir = Join-Path $DistDir $Name
$ffmpegExe = Get-ChildItem -Path (Join-Path $PWD "tools") -Recurse -Filter ffmpeg.exe | Select-Object -First 1
if (-not $ffmpegExe) {
    throw "ffmpeg.exe not found under .\tools"
}
$ffmpegBin = Split-Path $ffmpegExe.FullName
$toolsDst = Join-Path $appDir "tools\ffmpeg"
New-Item -ItemType Directory -Force -Path $toolsDst | Out-Null
Copy-Item -Recurse -Force (Join-Path $ffmpegBin "*") $toolsDst

$configPath = Join-Path $appDir "config.json"
if (-not (Test-Path $configPath)) {
    @'
{
  "input_dir": "D:/path/to/input",
  "output_dir": "D:/path/to/output",
  "db_path": "",
  "input_ext": ".ts",
  "crop_bottom_px": 100,
  "source_width": 1920,
  "source_height": 1080,
  "output_width": 1920,
  "output_height": 1080,
  "encode_mode": "quality",
  "crf": 18,
  "preset": "slow",
  "nvenc_cq": 18,
  "nvenc_preset": "p4",
  "audio_flags": ["-c:a", "copy"],
  "workers": 4,
  "timeout_seconds": 7200,
  "limit": null,
  "shard_index": null,
  "shard_count": null
}
'@ | Set-Content -Encoding UTF8 $configPath
}

@"
@echo off
setlocal
cd /d %~dp0
VideoBatchProcessor.exe %*
"@ | Set-Content -Encoding ASCII (Join-Path $appDir "run.cmd")

$releaseDir = Join-Path $PWD "release"
New-Item -ItemType Directory -Force -Path $releaseDir | Out-Null
$zipPath = Join-Path $releaseDir "$Name-win-portable.zip"
if (Test-Path $zipPath) {
    Remove-Item -Force $zipPath
}
Compress-Archive -Path $appDir -DestinationPath $zipPath

Write-Host "Package ready at $appDir"
Write-Host "ZIP created at $zipPath"
