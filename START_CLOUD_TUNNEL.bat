@echo off
setlocal EnableExtensions
pushd "%~dp0" >nul 2>&1
title Nunes Recruitment Cloud Tunnel

echo ============================================================
echo  NUNES RECRUITMENT - LIVE CLOUD ACCESS TUNNEL
echo ============================================================
echo Connecting local recruitment backend (port 5286) to Vercel...
echo.

if not exist "cloudflared.exe" (
    echo Downloading cloudflared.exe...
    powershell -NoProfile -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe' -OutFile 'cloudflared.exe'"
)

echo Starting Cloudflare Tunnel...
cloudflared.exe tunnel --url http://127.0.0.1:5286

pause
popd >nul 2>&1
