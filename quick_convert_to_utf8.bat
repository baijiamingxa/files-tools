@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul

if "%~1"=="" (
    echo.
    echo   Drag one or more files / folders onto this .bat file.
    echo   They will be converted to UTF-8 (no BOM) into a new "_utf8" folder.
    echo   Original files are never touched.
    echo.
    pause
    exit /b 0
)

set "PY=py.exe"
where py.exe >nul 2>nul || set "PY=python.exe"

echo Converting to UTF-8 (no BOM), originals stay untouched...
echo.
"%PY%" "%~dp0txt_encoder_cli.py" %* --target utf-8 --bom none --ext all --report "%~dp0last_report.csv"
echo.
echo Report saved to: %~dp0last_report.csv
pause
