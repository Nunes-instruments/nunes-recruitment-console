@echo off
setlocal EnableExtensions
pushd "%~dp0" >nul 2>&1
title Nunes Recruitment V11.11.20 Diagnostics

echo ============================================================
echo  NUNES V11.11.20 - SINGLE SESSION / LIVE DASHBOARD
echo ============================================================
echo.

echo [1] Version
powershell -NoProfile -NonInteractive -Command "try{Invoke-RestMethod -TimeoutSec 4 http://127.0.0.1:5286/version|ConvertTo-Json -Depth 8}catch{Write-Host $_.Exception.Message -ForegroundColor Red}"
echo.

echo [2] Chrome
powershell -NoProfile -NonInteractive -Command "try{Invoke-RestMethod -TimeoutSec 5 http://127.0.0.1:5286/api/chrome/diagnostics|ConvertTo-Json -Depth 16}catch{Write-Host $_.Exception.Message -ForegroundColor Red}"
echo.

echo [3] Current roles
powershell -NoProfile -NonInteractive -Command "try{Invoke-RestMethod -TimeoutSec 8 http://127.0.0.1:5286/api/role-discovery/diagnostics|ConvertTo-Json -Depth 18}catch{Write-Host $_.Exception.Message -ForegroundColor Red}"
echo.

echo [4] Candidate permission
powershell -NoProfile -NonInteractive -Command "try{Invoke-RestMethod -TimeoutSec 5 http://127.0.0.1:5286/api/indeed/permission-status|ConvertTo-Json -Depth 12}catch{Write-Host $_.Exception.Message -ForegroundColor Red}"
echo.

echo EXPECTED:
echo   One Recruitment Chrome window only.
echo   One Jobs tab only.
echo   Open / Paused / Flagged roles shown.
echo   No repeated chooser tabs.
echo.
pause
