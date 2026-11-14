@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul

set "LOG_FILE=%~dp0plugin.log"
set "CONFIG_FILE=%~dp0project_path.txt"
set "PROJECT_DIR="

REM 1. Check environment variable ANTIGRAVITY_PROJECT_DIR
if defined ANTIGRAVITY_PROJECT_DIR (
    if exist "%ANTIGRAVITY_PROJECT_DIR%\src\antigravity_monitor" (
        set "PROJECT_DIR=%ANTIGRAVITY_PROJECT_DIR%"
    )
)

REM 2. Check cached config file (project_path.txt)
if not defined PROJECT_DIR (
    if exist "%CONFIG_FILE%" (
        set /p SAVED_PATH=<"%CONFIG_FILE%"
        if exist "!SAVED_PATH!\src\antigravity_monitor" (
            set "PROJECT_DIR=!SAVED_PATH!"
        )
    )
)

REM 3. Check relative path if running from repo clone
if not defined PROJECT_DIR (
    if exist "%~dp0..\..\src\antigravity_monitor" (
        pushd "%~dp0..\.."
        set "PROJECT_DIR=!CD!"
        popd
    )
)

if not defined PROJECT_DIR (
    echo [%DATE% %TIME%] [ERROR] Could not resolve Antigravity project directory. >> "%LOG_FILE%"
    exit /b 1
)

cd /d "%PROJECT_DIR%"
set "PYTHONPATH=%PROJECT_DIR%"
set "ANTIGRAVITY_LOG_FILE=%LOG_FILE%"

REM Determine Python executable (prefer windowless pythonw.exe)
if defined ANTIGRAVITY_PYTHON (
    set "PY_CMD=%ANTIGRAVITY_PYTHON%"
) else if exist "%PROJECT_DIR%\.venv\Scripts\pythonw.exe" (
    set "PY_CMD=%PROJECT_DIR%\.venv\Scripts\pythonw.exe"
) else if exist "%PROJECT_DIR%\.venv\Scripts\python.exe" (
    set "PY_CMD=%PROJECT_DIR%\.venv\Scripts\python.exe"
) else (
    set "PY_CMD=python"
)

echo [%DATE% %TIME%] Starting Antigravity Stream Deck plugin in "%PROJECT_DIR%" using "%PY_CMD%" >> "%LOG_FILE%"
"%PY_CMD%" -u -m src.antigravity_monitor.streamdeck_bridge %*
if errorlevel 1 (
    echo [%DATE% %TIME%] [ERROR] Antigravity Stream Deck plugin exited with code !ERRORLEVEL!. >> "%LOG_FILE%"
    exit /b !ERRORLEVEL!
)

