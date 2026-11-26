@echo off
setlocal EnableExtensions
title Volundr - Launcher
cd /d "%~dp0"

set "VENV_DIR=%~dp0.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "VENV_PYW=%VENV_DIR%\Scripts\pythonw.exe"
set "GUI_ENTRY=%~dp0volundr_gui.pyw"

if not exist "%VENV_PY%" goto :bootstrap
goto :check_deps

:bootstrap
echo ========================================================================
echo  Volundr - First-Time Bootstrap
echo ========================================================================
echo.

set "SYS_PY="
where py >nul 2>&1
if not errorlevel 1 (
    py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
    if not errorlevel 1 set "SYS_PY=py -3"
)
if not defined SYS_PY (
    where python >nul 2>&1
    if not errorlevel 1 (
        python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
        if not errorlevel 1 set "SYS_PY=python"
    )
)

if not defined SYS_PY (
    echo [ERROR] Python 3.11 or newer was not found on your system.
    echo.
    echo Install Python from:
    echo   https://www.python.org/downloads/
    echo   ^(Make sure to check "Add python.exe to PATH" during installation.^)
    echo.
    echo Or install via winget in PowerShell:
    echo   winget install -e --id Python.Python.3.13
    echo.
    pause
    exit /b 1
)

echo [1/3] Creating virtual environment ^(.venv^)...
%SYS_PY% -m venv "%VENV_DIR%"
if errorlevel 1 (
    echo [ERROR] Failed to create virtual environment in "%VENV_DIR%".
    pause
    exit /b 1
)

echo [2/3] Installing core dependencies ^(requirements.txt^)...
"%VENV_PY%" -m pip install --upgrade pip >nul 2>&1
"%VENV_PY%" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo [ERROR] Failed to install core dependencies from requirements.txt.
    pause
    exit /b 1
)

echo [3/3] Installing desktop GUI dependencies ^(requirements-gui.txt^)...
"%VENV_PY%" -m pip install -r "%~dp0requirements-gui.txt"
if errorlevel 1 (
    echo [ERROR] Failed to install GUI dependencies from requirements-gui.txt.
    pause
    exit /b 1
)
echo.
echo Bootstrap complete! Launching Volundr...
goto :launch

:check_deps
"%VENV_PY%" -c "import watchdog, rich" >nul 2>&1
if errorlevel 1 (
    echo Installing missing core dependencies ^(requirements.txt^)...
    "%VENV_PY%" -m pip install -r "%~dp0requirements.txt"
    if errorlevel 1 (
        echo [ERROR] Failed to install requirements.txt.
        pause
        exit /b 1
    )
)

"%VENV_PY%" -c "import PySide6" >nul 2>&1
if errorlevel 1 (
    echo Installing desktop GUI dependencies ^(requirements-gui.txt^)...
    "%VENV_PY%" -m pip install -r "%~dp0requirements-gui.txt"
    if errorlevel 1 (
        echo [ERROR] Failed to install requirements-gui.txt.
        pause
        exit /b 1
    )
)

:launch
if exist "%VENV_PYW%" (
    start "" "%VENV_PYW%" "%GUI_ENTRY%" %*
    exit /b 0
)

start "" "%VENV_PY%" "%~dp0main.py" gui %*
exit /b 0
