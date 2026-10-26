@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set "ACTION=--install"
if /i "%~1"=="--uninstall" set "ACTION=--uninstall"
where py.exe >nul 2>nul
if errorlevel 1 goto fallback
py.exe -3 -X utf8 "%~dp0windows_integration.py" %ACTION%
goto done
:fallback
python.exe -X utf8 "%~dp0windows_integration.py" %ACTION%
:done
set "RESULT=%ERRORLEVEL%"
echo.
pause
exit /b %RESULT%
