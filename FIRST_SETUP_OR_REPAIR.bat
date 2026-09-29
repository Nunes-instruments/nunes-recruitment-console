@echo off
setlocal EnableExtensions
pushd "%~dp0" >nul 2>&1
title Nunes Recruitment V11.11.24 - First Setup / Repair

echo ============================================================
echo  NUNES RECRUITMENT V11.11.24 - FIRST SETUP / REPAIR
echo ============================================================
echo.
echo This window is intentionally visible so setup errors can be read.
echo After setup, use START_NUNES_SILENT.vbs for normal daily opening.
echo.

call START.bat
if errorlevel 1 (
  echo.
  echo Startup/setup failed. Run DIAGNOSE_FINAL_V11_11_24.bat.
  pause
)
