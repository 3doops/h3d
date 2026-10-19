# Keeps the asset queue runner alive: runs runner.py, and if it exits (crash, killed child, uv hiccup) waits a minute
# and starts it again — until work\runner.stop exists (stop_runner.ps1 creates it).
# Started hidden by start_runner.ps1; arguments are passed through to runner.py.
$pipe = $PSScriptRoot
$work = Join-Path (Split-Path $pipe -Parent) 'work'
$stop = Join-Path $work 'runner.stop'
$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) { $uv = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links\uv.exe' }
Set-Content -Path (Join-Path $work 'supervisor.pid') -Value $PID -Encoding ascii
while (-not (Test-Path $stop)) {
    $argList = @('run', '--no-project', '--python', '3.12', '--with', 'requests', '--with', 'pillow', '--with', 'numpy',
                 'python', '-I', '-u', (Join-Path $pipe 'runner.py')) + $args
    $p = Start-Process -FilePath $uv -ArgumentList $argList -WorkingDirectory $pipe -WindowStyle Hidden -PassThru -Wait `
        -RedirectStandardOutput (Join-Path $work 'runner.out') -RedirectStandardError (Join-Path $work 'runner.err')
    $line = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + " supervisor: runner exited with code $($p.ExitCode)"
    Add-Content -Path (Join-Path $work 'runner.log') -Value $line -Encoding utf8
    if (Test-Path $stop) { break }
    Start-Sleep -Seconds 60
}
Remove-Item (Join-Path $work 'supervisor.pid') -ErrorAction SilentlyContinue
