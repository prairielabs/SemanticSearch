param(
    [int]$Tail = 80
)

$ErrorActionPreference = "Stop"

$payload = $PSScriptRoot
$logs = Join-Path $payload "logs"
$liveLog = Join-Path $logs "live.log"

New-Item -ItemType Directory -Path $logs -Force | Out-Null
if (-not (Test-Path -LiteralPath $liveLog)) {
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($liveLog, "", $utf8NoBom)
}

$host.UI.RawUI.WindowTitle = "Prairie Search Live Logs"
Write-Host "Prairie Search live harness log"
Write-Host $liveLog
Write-Host ""
Get-Content -LiteralPath $liveLog -Tail $Tail -Wait
