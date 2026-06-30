param(
    [int]$Port = 8765,
    [switch]$Reset
)

$ErrorActionPreference = "Stop"

$payload = $PSScriptRoot
$config = Join-Path $payload "config"
$history = Join-Path $payload "history"
$logs = Join-Path $payload "logs"
$base = Join-Path $payload "base.html"
$index = Join-Path $payload "index.html"
$keyFile = Join-Path $config "mistral_api_key.txt"

New-Item -ItemType Directory -Path $config -Force | Out-Null
New-Item -ItemType Directory -Path $history -Force | Out-Null
New-Item -ItemType Directory -Path $logs -Force | Out-Null

if (-not (Test-Path -LiteralPath $keyFile)) {
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($keyFile, "PASTE_MISTRAL_API_KEY_HERE`n", $utf8NoBom)
}

if (($Reset -or -not (Test-Path -LiteralPath $index)) -and (Test-Path -LiteralPath $base)) {
    Copy-Item -LiteralPath $base -Destination $index -Force
}

$keyText = (Get-Content -LiteralPath $keyFile -Raw -ErrorAction SilentlyContinue).Trim()
if ($keyText.Length -eq 0 -or $keyText -eq "PASTE_MISTRAL_API_KEY_HERE") {
    Start-Process -FilePath "notepad.exe" -ArgumentList "`"$keyFile`""
}

$pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
$usePyLauncher = $false
if (-not $pythonCommand) {
    $pythonCommand = Get-Command py.exe -ErrorAction SilentlyContinue
    $usePyLauncher = $true
}
if (-not $pythonCommand) {
    throw "Python was not found. Install Python 3, then run the Semantic Search shortcut again."
}

$health = "http://127.0.0.1:$Port/health"
$serverAlreadyRunning = $false
try {
    $response = Invoke-WebRequest -UseBasicParsing -Uri $health -TimeoutSec 1
    $serverAlreadyRunning = ($response.StatusCode -eq 200)
} catch {
    $serverAlreadyRunning = $false
}

if (-not $serverAlreadyRunning) {
    $script = Join-Path $payload "semantic_harness.py"
    if ($usePyLauncher) {
        $arguments = @("-3", "`"$script`"", "--port", "$Port")
    } else {
        $arguments = @("`"$script`"", "--port", "$Port")
    }
    Start-Process -FilePath $pythonCommand.Source -ArgumentList $arguments -WorkingDirectory $payload -WindowStyle Hidden

    $ready = $false
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 250
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $health -TimeoutSec 1
            if ($response.StatusCode -eq 200) {
                $ready = $true
                break
            }
        } catch {
            $ready = $false
        }
    }
    if (-not $ready) {
        throw "Semantic Search harness did not start on port $Port."
    }
}

Start-Process "http://127.0.0.1:$Port/index.html"
