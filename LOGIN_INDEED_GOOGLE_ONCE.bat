@echo off
setlocal EnableExtensions EnableDelayedExpansion
pushd "%~dp0" >nul 2>&1
title Nunes Recruitment V11.11.24 - Safe Indeed Google Login

set "EXPECTED_VERSION=V11.11.24"
set "EXPECTED_GOOGLE_ACCOUNT=nuneslead@gmail.com"
set "API=http://127.0.0.1:5286"
set "READY=0"

echo ============================================================
echo  NUNES RECRUITMENT - ONE-TIME SAFE INDEED GOOGLE LOGIN
echo ============================================================
echo.
echo Google / Indeed account: %EXPECTED_GOOGLE_ACCOUNT%
echo.
echo This tool verifies the CURRENT recruitment backend first.
echo If an older version is still using port 5286, it will replace it
echo automatically before opening the safe Google/Indeed login.
echo.

REM ------------------------------------------------------------
REM 1. Is the correct backend already running?
REM ------------------------------------------------------------
for /f "usebackq delims=" %%V in (`powershell -NoProfile -NonInteractive -Command ^
  "try { $v=Invoke-RestMethod -TimeoutSec 2 '%API%/version'; if($v.version){[Console]::Write($v.version)} } catch {}"`) do (
    set "RUNNING_VERSION=%%V"
)

if /I "!RUNNING_VERSION!"=="%EXPECTED_VERSION%" (
    set "READY=1"
    echo [OK] Current backend is already running: !RUNNING_VERSION!
) else (
    if defined RUNNING_VERSION (
        echo [INFO] Older/different backend detected: !RUNNING_VERSION!
    ) else (
        echo [INFO] Recruitment backend is not ready on port 5286.
    )
    echo [INFO] Starting %EXPECTED_VERSION% from this folder...
    echo.

    REM START.bat already kills only the app's fixed ports when the version
    REM does not match, then starts this package's current backend/frontend.
    call "%~dp0START.bat"

    REM ----------------------------------------------------------
    REM 2. Wait up to ~45 seconds for the exact current API.
    REM ----------------------------------------------------------
    for /L %%N in (1,1,45) do (
        if "!READY!"=="0" (
            set "FOUND_VERSION="
            for /f "usebackq delims=" %%V in (`powershell -NoProfile -NonInteractive -Command ^
              "try { $v=Invoke-RestMethod -TimeoutSec 1 '%API%/version'; if($v.version){[Console]::Write($v.version)} } catch {}"`) do (
                set "FOUND_VERSION=%%V"
            )

            if /I "!FOUND_VERSION!"=="%EXPECTED_VERSION%" (
                set "READY=1"
            ) else (
                >nul 2>&1 timeout /t 1 /nobreak
            )
        )
    )
)

if not "!READY!"=="1" (
    echo.
    echo [ERROR] %EXPECTED_VERSION% backend did not become ready.
    echo.
    echo Run:
    echo   DIAGNOSE_FINAL_V11_11_24.bat
    echo.
    pause
    exit /b 1
)

echo [OK] %EXPECTED_VERSION% API is ready.
echo.
echo [2/2] Opening SAFE Indeed login...
echo       Remote debugging will be OFF during this one-time login.
echo.

REM ------------------------------------------------------------
REM 3. Call the route. If an unexpected 404 occurs, show actual version.
REM ------------------------------------------------------------
powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "try {" ^
  "  $v=Invoke-RestMethod -TimeoutSec 3 '%API%/version';" ^
  "  if($v.version -ne '%EXPECTED_VERSION%'){ throw ('Wrong backend version: ' + $v.version) };" ^
  "  $r=Invoke-RestMethod -Method Post -TimeoutSec 25 '%API%/api/indeed/safe-login';" ^
  "  $r | ConvertTo-Json -Depth 10" ^
  "} catch {" ^
  "  Write-Host ('SAFE LOGIN ERROR: ' + $_.Exception.Message) -ForegroundColor Red;" ^
  "  try { $v=Invoke-RestMethod -TimeoutSec 2 '%API%/version'; Write-Host ('Backend version: ' + $v.version) -ForegroundColor Yellow } catch {};" ^
  "  exit 1" ^
  "}"

if errorlevel 1 (
    echo.
    echo Safe login could not start.
    echo Run DIAGNOSE_FINAL_V11_11_24.bat and send the output.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  SAFE LOGIN CHROME IS NOW OPEN
echo ============================================================
echo.
echo 1. Choose/sign in with %EXPECTED_GOOGLE_ACCOUNT% in Google / Indeed.
echo 2. Confirm the correct Employer account opens.
echo 3. CLOSE that Recruitment Chrome window completely.
echo 4. Do NOT close the NUNES Recruitment Console.
echo.
echo After the safe-login Chrome closes, the console will reopen the
echo SAME saved profile for live monitoring automatically.
echo.
pause
