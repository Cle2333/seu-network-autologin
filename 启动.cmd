@echo off
rem Keep this file ASCII-only: cmd.exe parses batch files with the console code page.
rem All Chinese output comes from the program itself.
setlocal enabledelayedexpansion
cd /d "%~dp0"

rem Pick a Python whose tkinter actually works. python.org installers ship a working
rem Tk; the Microsoft Store stub often does not, and very old 8.5 builds fail to draw
rem Label/Entry widgets.
set "PYTHON="
for %%P in ("py -3" "python") do (
    if not defined PYTHON (
        %%~P -c "import tkinter,sys;sys.exit(0 if float(tkinter.TkVersion)>=8.6 else 1)" >nul 2>&1
        if not errorlevel 1 set "PYTHON=%%~P"
    )
)

if not defined PYTHON (
    rem No usable GUI python. Fall back to a working interpreter for the CLI so the
    rem user is not stuck with nothing.
    where py >nul 2>&1 && set "PYTHON=py -3"
    if not defined PYTHON (
        where python >nul 2>&1 && set "PYTHON=python"
    )
    if defined PYTHON (
        echo.
        echo The installed Python cannot draw windows ^(tkinter/Tk is missing or too old^).
        echo Falling back to the command line.
        echo.
        echo   1. Configure your account:  %PYTHON% seu_net.py init
        echo   2. Check status:            %PYTHON% seu_net.py status
        echo   3. Log in:                  %PYTHON% seu_net.py
        echo.
        echo To get the window instead, install Python 3.12+ from python.org
        echo ^(tick "Add python.exe to PATH" during setup^).
        echo.
        pause
        exit /b 1
    )
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
endlocal
