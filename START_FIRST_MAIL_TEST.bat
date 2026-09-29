@echo off
setlocal
title NUNES Recruitment - First Mail Test
cd /d "%~dp0"

echo ============================================================
echo  NUNES RECRUITMENT - FIRST MAIL / ALL CANDIDATES TEST
echo ============================================================
echo.
echo This launcher uses:
echo   app_first_mail.py
echo   chrome_cdp_all_candidates.py
echo.
echo Existing app.py and chrome_cdp.py are NOT changed.
echo.
echo Starting separate test backend on the configured API port...
echo.

python app_first_mail.py

echo.
echo ============================================================
echo  Test backend stopped.
echo ============================================================
pause
