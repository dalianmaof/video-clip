param(
    [string]$Name = "VideoBatchProcessor"
)

$ErrorActionPreference = "Stop"
$env:Path = "$env:USERPROFILE\.cargo\bin;$env:Path"

Write-Host "Building Tauri app..."
npm run tauri build

Write-Host ""
Write-Host "Build complete. Artifacts:"
Get-ChildItem -Path "src-tauri/target/release/bundle" -Recurse -Include "*.msi","*.exe","*.zip" |`
    Select-Object FullName, @{N='Size(MB)';E={[math]::Round($_.Length/1MB,1)}}
