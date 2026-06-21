$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = "C:\Users\91801\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$npm = "C:\Program Files\nodejs\npm.cmd"

function Start-AssistantProcess {
    param(
        [string]$FileName,
        [string]$Arguments,
        [string]$WorkingDirectory
    )

    $processInfo = New-Object System.Diagnostics.ProcessStartInfo
    $processInfo.FileName = $FileName
    $processInfo.Arguments = $Arguments
    $processInfo.WorkingDirectory = $WorkingDirectory
    $processInfo.UseShellExecute = $true
    $processInfo.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
    [System.Diagnostics.Process]::Start($processInfo) | Out-Null
}

$backendPort = netstat -ano | Select-String ":8000"
if ($backendPort) {
    Write-Host "Port 8000 is already in use. Stop the old backend before restarting if the stream route is stale."
} else {
    Start-AssistantProcess -FileName $python -Arguments "run_server.py" -WorkingDirectory "$root\backend"
}

Start-AssistantProcess -FileName $npm -Arguments "run dev" -WorkingDirectory "$root\frontend"

Write-Host "Backend:  http://localhost:8000"
Write-Host "Frontend: http://localhost:5173"
