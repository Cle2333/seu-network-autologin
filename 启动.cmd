@echo off
rem Keep this file ASCII-only: cmd.exe parses batch files with the console code page.
rem All Chinese output comes from the program itself.
cd /d "%~dp0"

set "PYTHON="
where py >nul 2>&1 && set "PYTHON=py -3"
if not defined PYTHON (
    where python >nul 2>&1 && set "PYTHON=python"
)

if not defined PYTHON (
    echo Python 3 not found.
    echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
    echo.
    pause
    exit /b 1
)

%PYTHON% seu_net.py gui
if errorlevel 1 (
    echo.
    echo Exited with an error. Run this to diagnose:
    echo   %PYTHON% seu_net.py doctor
    echo.
    pause
)
