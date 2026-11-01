$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Source = Join-Path $Root "src"
$Assets = Join-Path $Root "assets"
$Outputs = Join-Path $Root "outputs"
$Build = Join-Path $Root "build"

New-Item -ItemType Directory -Force -Path $Outputs,$Build | Out-Null

python (Join-Path $Root "create_icon.py")
python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name "DataBackupAssistant" `
    --icon (Join-Path $Assets "app.ico") `
    --version-file (Join-Path $Root "version_info.txt") `
    --add-data "$Assets\app.png;." `
    --add-data "$Assets\app.ico;." `
    --distpath $Outputs `
    --workpath $Build `
    --specpath $Build `
    (Join-Path $Source "app.py")

Write-Host "Built: $(Join-Path $Outputs 'DataBackupAssistant.exe')"

