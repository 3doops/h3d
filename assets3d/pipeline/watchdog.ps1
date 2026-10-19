# Watchdog for the scheduled task "HotA3D-AssetRunner": (re)starts the asset runner after a reboot or if both the
# supervisor and the runner died — unless it was stopped on purpose (work\runner.stop from stop_runner.ps1).
# Install (once):  schtasks /Create /TN HotA3D-AssetRunner /SC MINUTE /MO 15 /F /TR "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File C:\prj\hota-decompile\godot\assets3d\pipeline\watchdog.ps1"
# Remove:          schtasks /Delete /TN HotA3D-AssetRunner /F
$work = Join-Path (Split-Path $PSScriptRoot -Parent) 'work'
if (Test-Path (Join-Path $work 'runner.stop')) { exit 0 }
$argsFile = Join-Path $work 'runner.args'
$saved = @()
if (Test-Path $argsFile) { $saved = @(Get-Content $argsFile | Where-Object { $_ -ne '' }) }
& (Join-Path $PSScriptRoot 'start_runner.ps1') @saved
