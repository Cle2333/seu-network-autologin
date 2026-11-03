@echo off
rem Keep this file ASCII-only: cmd.exe parses batch files with the console code page,
rem so non-ASCII text here would be garbled on a Chinese Windows.
rem Remove the two scheduled tasks (asks for elevation). Also restores the
rem Windows "sign in to network" popup; pass --keep-popup to leave it alone.
cd /d "%~dp0"
if exist "seu-autologin.exe" (
    "seu-autologin.exe" --uninstall --pause
) else (
    echo [ERROR] seu-autologin.exe not found in this folder.
    pause
)
