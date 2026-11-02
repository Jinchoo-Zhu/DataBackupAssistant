$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Outputs = Join-Path $Root "outputs"
$Build = Join-Path $Root "build"
$Spec = Join-Path $Root "DataBackupAssistant.spec"

New-Item -ItemType Directory -Force -Path $Outputs,$Build | Out-Null

python (Join-Path $Root "create_icon.py")
python -m PyInstaller `
    --noconfirm `
    --clean `
    --distpath $Outputs `
    --workpath $Build `
    $Spec

Write-Host "Built: $(Join-Path $Outputs '数据自动备份助手.exe')"

