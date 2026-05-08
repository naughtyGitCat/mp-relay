@echo off
chcp 65001 >nul
title mp-relay
cd /d "%~dp0"

rem ------------------------------------------------------------
rem mp-relay launcher (interactive / non-service mode)
rem
rem Use this if you DIDN'T install as a Windows service. It runs
rem uvicorn in the foreground so you can see logs in the console.
rem ------------------------------------------------------------

rem Path-length sanity check. Spaces are fine; Python handles them.
setlocal enabledelayedexpansion
set "_dir=%cd%"
if not "!_dir!"=="!_dir:~,240!" (
    echo [ERROR] Install path > 240 chars. Reinstall to a shorter path.
    pause
    exit /b 1
)
endlocal

if not exist "%~dp0Python\python.exe" (
    echo [ERROR] Embedded Python not found at %~dp0Python\python.exe
    echo The install may be corrupt — try reinstalling.
    pause
    exit /b 1
)

if not exist "%~dp0.env" (
    echo [WARNING] .env not found. Copying from .env.example...
    copy "%~dp0.env.example" "%~dp0.env" >nul
    echo Edit %~dp0.env to set MoviePilot / qBittorrent / mdcx paths,
    echo then run this script again.
    notepad "%~dp0.env"
    pause
    exit /b 1
)

set "PYTHONUNBUFFERED=1"
set "PYTHONIOENCODING=utf-8"

echo Starting mp-relay on http://127.0.0.1:5000  (Ctrl+C to stop)
start "" "http://127.0.0.1:5000"
"%~dp0Python\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 5000
pause
