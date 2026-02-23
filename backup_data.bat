@echo off
setlocal

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0backup_data.ps1"
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
  echo.
  echo Backup failed. Exit code: %EXIT_CODE%
  pause
  exit /b %EXIT_CODE%
)

echo.
echo Backup finished.
pause
