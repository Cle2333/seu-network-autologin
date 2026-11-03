@echo off
rem Keep this file ASCII-only: cmd.exe parses batch files with the console code page,
rem so non-ASCII text here would be garbled on a Chinese Windows.
rem Show current status: config / network / scheduled tasks / recent log.
cd /d "%~dp0"
if exist "seu-autologin.exe" (
    "seu-autologin.exe" --status --pause
) else (
    echo [ERROR] seu-autologin.exe not found in this folder.
    pause
)
