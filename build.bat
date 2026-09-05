@echo off
rem Nuitka build wrapper for the Vanta Launcher (Inpriv Labs).
rem Usage: build.bat [--debug]
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 build.py %*
) else (
    python build.py %*
)
endlocal
