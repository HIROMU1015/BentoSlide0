@echo off
setlocal
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass ^
  -File "%~dp0scripts\stop_bento_editor.ps1" %*
set "exitCode=%ERRORLEVEL%"
if not "%exitCode%"=="0" (
  echo.
  echo Bento Work editor launcher failed. Review the message above.
  if /I not "%BENTO_EDITOR_NO_PAUSE%"=="1" pause
)
exit /b %exitCode%
