param(
    [switch]$NoLaunch
)

$ErrorActionPreference = "Stop"

$installerLayer = $PSScriptRoot
$rootSource = Split-Path -Parent $installerLayer
$desktop = [Environment]::GetFolderPath("Desktop")
$prairieRoot = Join-Path $desktop "Prairie"
$appRoot = Join-Path $prairieRoot "SemanticSearch"
$rootTarget = Join-Path $appRoot "Root"

New-Item -ItemType Directory -Path $prairieRoot -Force | Out-Null
New-Item -ItemType Directory -Path $appRoot -Force | Out-Null
New-Item -ItemType Directory -Path $rootTarget -Force | Out-Null

Get-ChildItem -LiteralPath $rootSource | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $rootTarget -Recurse -Force
}

$payload = Join-Path $rootTarget "Layer_1_Installer\Layer_2_Preload\Layer_3_Payload"
$config = Join-Path $payload "config"
$history = Join-Path $payload "history"
$logs = Join-Path $payload "logs"
New-Item -ItemType Directory -Path $config -Force | Out-Null
New-Item -ItemType Directory -Path $history -Force | Out-Null
New-Item -ItemType Directory -Path $logs -Force | Out-Null

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)

$keyFile = Join-Path $config "mistral_api_key.txt"
if (-not (Test-Path -LiteralPath $keyFile)) {
    [System.IO.File]::WriteAllText($keyFile, "PASTE_MISTRAL_API_KEY_HERE`n", $utf8NoBom)
}

$modelFile = Join-Path $config "mistral_model.txt"
if (-not (Test-Path -LiteralPath $modelFile)) {
    [System.IO.File]::WriteAllText($modelFile, "mistral-medium-3-5`n", $utf8NoBom)
}

$base = Join-Path $payload "base.html"
$index = Join-Path $payload "index.html"
if (Test-Path -LiteralPath $base) {
    Copy-Item -LiteralPath $base -Destination $index -Force
}

$launcher = Join-Path $payload "Launch-SemanticSearch.ps1"
$shortcutPath = Join-Path $desktop "Semantic Search.lnk"
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$launcher`""
$shortcut.WorkingDirectory = $payload
$shortcut.IconLocation = Join-Path $env:SystemRoot "System32\shell32.dll,23"
$shortcut.Description = "Semantic Search, powered by Mistral"
$shortcut.Save()

Write-Host "Installed Semantic Search to: $appRoot"
Write-Host "Created desktop shortcut: $shortcutPath"
Write-Host "Mistral API key file: $keyFile"

if (-not $NoLaunch) {
    & $launcher
}
