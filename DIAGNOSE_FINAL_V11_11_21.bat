@echo off
setlocal EnableExtensions
pushd "%~dp0" >nul 2>&1
title Nunes Recruitment V11.11.21 Diagnostics

echo ============================================================
echo  NUNES V11.11.21 - PROFESSIONAL MEDIUM UI
echo ============================================================
echo.

echo [1] Version
powershell -NoProfile -NonInteractive -Command "try{Invoke-RestMethod -TimeoutSec 4 http://127.0.0.1:5286/version|ConvertTo-Json -Depth 8}catch{Write-Host $_.Exception.Message -ForegroundColor Red}"
echo.

echo [2] Dashboard lite API
powershell -NoProfile -NonInteractive -Command "try{Invoke-RestMethod -TimeoutSec 5 'http://127.0.0.1:5286/api/dashboard?lite=1'|ConvertTo-Json -Depth 7}catch{Write-Host $_.Exception.Message -ForegroundColor Red}"
echo.

echo [3] Recruitment overview
powershell -NoProfile -NonInteractive -Command "try{Invoke-RestMethod -TimeoutSec 7 'http://127.0.0.1:5286/api/recruitment/overview?roles=6&recent=8'|ConvertTo-Json -Depth 10}catch{Write-Host $_.Exception.Message -ForegroundColor Red}"
echo.

echo [4] Role discovery
powershell -NoProfile -NonInteractive -Command "try{Invoke-RestMethod -TimeoutSec 7 http://127.0.0.1:5286/api/role-discovery/diagnostics|ConvertTo-Json -Depth 14}catch{Write-Host $_.Exception.Message -ForegroundColor Red}"
echo.

echo EXPECTED:
echo   Version V11.11.21
echo   Existing live data remains wired.
echo   UI text is medium-sized and readable.
echo   No workflow/backend behavior changes in this release.
echo.
pause
