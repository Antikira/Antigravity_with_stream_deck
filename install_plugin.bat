@echo off
setlocal
chcp 65001 >nul

cd /d "%~dp0"

where uv >nul 2>&1
if errorlevel 1 (
    echo [ERROR] uv command not found. Please install uv first: https://docs.astral.sh/uv/
    exit /b 1
)

set "IS_UNINSTALL=0"
for %%A in (%*) do (
    if /I "%%~A"=="--uninstall" set "IS_UNINSTALL=1"
)

if "%IS_UNINSTALL%"=="1" (
    echo Uninstalling Antigravity Stream Deck plugin...
    if exist "%~dp0.venv\Scripts\python.exe" (
        "%~dp0.venv\Scripts\python.exe" -m src.antigravity_monitor.installer %*
    ) else (
        uv run --no-sync python -m src.antigravity_monitor.installer %*
    )
    if errorlevel 1 (
        echo [ERROR] Plugin uninstallation failed.
        exit /b 1
    )
    echo Uninstallation completed successfully.
    endlocal
    exit /b 0
)

set "VENV_NEED_CREATE=0"
if not exist "%~dp0.venv\Scripts\python.exe" (
    set "VENV_NEED_CREATE=1"
) else if not exist "%~dp0.venv\Scripts\pythonw.exe" (
    set "VENV_NEED_CREATE=1"
) else (
    "%~dp0.venv\Scripts\python.exe" -c "import sys" >nul 2>&1
    if errorlevel 1 set "VENV_NEED_CREATE=1"
)

if "%VENV_NEED_CREATE%"=="1" (
    echo Creating isolated virtual environment via uv...
    uv venv --clear
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment via uv.
        exit /b 1
    )
)

echo Syncing runtime dependencies via uv...
uv sync --no-dev
if errorlevel 1 (
    echo [ERROR] Failed to sync dependencies via uv.
    exit /b 1
)

echo Installing Antigravity Stream Deck plugin...
uv run --no-sync python -m src.antigravity_monitor.installer --skip-venv %*
if errorlevel 1 (
    echo [ERROR] Plugin installation failed.
    exit /b 1
)

echo Installation completed successfully.
endlocal
