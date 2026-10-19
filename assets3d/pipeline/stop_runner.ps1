# Stops the asset queue runner: tells the supervisor not to restart it (work\runner.stop), then kills the supervisor
# and the runner process trees (codex / blender jobs in flight are killed; those items are simply redone later).
# The scheduled task (if installed) would start it again within 15 min: remove work\runner.stop only when you
# want it back, or delete the task: schtasks /Delete /TN HotA3D-AssetRunner /F
$work = Join-Path (Split-Path $PSScriptRoot -Parent) 'work'
Set-Content -Path (Join-Path $work 'runner.stop') -Value (Get-Date -Format s) -Encoding ascii
foreach ($f in 'supervisor.pid', 'runner.lock') {
    $pf = Join-Path $work $f
    if (Test-Path $pf) {
        $procId = [int](Get-Content $pf -Raw).Trim()
        & taskkill /PID $procId /T /F 2>$null | Out-Null
        Remove-Item $pf -Force -ErrorAction SilentlyContinue
        Write-Output "stopped $f pid $procId"
    }
}
