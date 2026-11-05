@echo off
chcp 65001 >nul 2>&1
setlocal
cd /d "%~dp0"
title ShiIDPhoto

rem ASCII-only on purpose: cmd.exe parses batch files with the system code page,
rem so any non-ASCII here is a latent encoding bug. Chinese messages come from launcher.py
rem (it overwrites this title as soon as config/portable.json is read).

set "PYTHON=%~dp0python-embed\python.exe"
set "LAUNCHER=%~dp0launcher.py"

if not exist "%PYTHON%" (
  echo.
  echo [ERROR] python-embed\python.exe not found.
  echo Please extract the whole archive again; do not copy this file alone.
  echo.
  pause
  exit /b 1
)

if not exist "%LAUNCHER%" (
  echo.
  echo [ERROR] launcher.py not found.
  echo The archive may be incompletely extracted; please extract again.
  echo.
  pause
  exit /b 1
)

set "PYTHONUTF8=1"
set "PYTHONNOUSERSITE=1"
set "HF_HUB_OFFLINE=1"
set "HF_HUB_DISABLE_TELEMETRY=1"
set "GRADIO_ANALYTICS_ENABLED=False"

"%PYTHON%" "%LAUNCHER%" %*
set "EXITCODE=%ERRORLEVEL%"

if not "%EXITCODE%"=="0" (
  echo.
  echo Exited with code %EXITCODE%
  echo See log: user-data\logs\webui.log
  echo.
  pause
)

exit /b %EXITCODE%
