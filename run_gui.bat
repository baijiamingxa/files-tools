@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
chcp 65001 >nul

set "PYW="
for %%C in (pyw.exe pythonw.exe) do (
    if not defined PYW (
        where %%C >nul 2>nul && set "PYW=%%C"
    )
)

if defined PYW (
    start "" !PYW! "%~dp0file_toolbox.py" %*
    exit /b 0
)

set "PY="
for %%C in (py.exe python.exe) do (
    if not defined PY (
        where %%C >nul 2>nul && set "PY=%%C"
    )
)

if defined PY (
    !PY! "%~dp0file_toolbox.py" %*
    exit /b %errorlevel%
)

echo.
echo   [ERROR] Python 3 not found on this computer.
echo   Install Python 3.8+ from https://www.python.org/downloads/
echo   and tick "Add python.exe to PATH" during setup.
echo.
pause
exit /b 1
