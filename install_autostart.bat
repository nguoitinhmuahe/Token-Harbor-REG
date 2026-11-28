@echo off
setlocal EnableExtensions
set "HERE=%~dp0"
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "VBS=%STARTUP%\tokenharbor_guard.vbs"
set "OLD=%STARTUP%\tokenharbor_guard.cmd"

if /I "%~1"=="remove" (
  if exist "%VBS%" del "%VBS%" && echo [ok] da go autostart || echo [i] khong co gi de go
  if exist "%OLD%" del "%OLD%"
  pause
  exit /b 0
)

if not exist "%HERE%guard_hidden.vbs" (
  echo [x] thieu "%HERE%guard_hidden.vbs"
  pause
  exit /b 1
)
copy /Y "%HERE%guard_hidden.vbs" "%VBS%" >nul
if exist "%OLD%" del "%OLD%"
echo [ok] da dat autostart (chay nen): %VBS%
echo      lan sau dang nhap, guard se tu chay -> panel + gateway, khong cua so.
echo      go bang: install_autostart.bat remove
pause
endlocal
