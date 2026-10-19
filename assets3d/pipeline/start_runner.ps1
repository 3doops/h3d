# Starts the asset queue runner under a supervisor (supervise.ps1) as a detached hidden process: it keeps running
# after the console/agent session ends and is restarted a minute after any crash. Safe to call repeatedly (does
# nothing if a supervisor or runner is alive). Clears work\runner.stop and remembers the options in
# work\runner.args for watchdog.ps1 (scheduled task "HotA3D-AssetRunner", every 15 min).
# usage: powershell -File godot\assets3d\pipeline\start_runner.ps1 [runner.py options, e.g. --max-priority 2]
# Logs: godot\assets3d\work\runner.log (+ runner.out/.err). Stop: stop_runner.ps1.
$ErrorActionPreference = 'Stop'
$pipe = $PSScriptRoot
$work = Join-Path (Split-Path $pipe -Parent) 'work'
New-Item -ItemType Directory -Force $work | Out-Null
foreach ($f in 'supervisor.pid', 'runner.lock') {
    $pf = Join-Path $work $f
    if (Test-Path $pf) {
        $old = [int](Get-Content $pf -Raw).Trim()
        if (Get-Process -Id $old -ErrorAction SilentlyContinue) {
            Write-Output "already running ($f pid $old)"
            exit 0
        }
        Remove-Item $pf -Force
    }
}
Remove-Item (Join-Path $work 'runner.stop') -ErrorAction SilentlyContinue
Set-Content -Path (Join-Path $work 'runner.args') -Value ($args -join "`n") -Encoding utf8
$argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden', '-File',
             (Join-Path $pipe 'supervise.ps1')) + $args
$p = Start-Process -FilePath 'powershell.exe' -ArgumentList $argList -WorkingDirectory $pipe -WindowStyle Hidden -PassThru
Write-Output "started supervisor pid $($p.Id); log: $work\runner.log"
