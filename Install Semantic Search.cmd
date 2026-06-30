@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Root\Layer_1_Installer\Install-SemanticSearch.ps1"
endlocal
