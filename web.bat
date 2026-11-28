@echo off
setlocal EnableExtensions
title Token Harbor - panel
chcp 65001 >nul

set "HERE=%~dp0"
set "SCRIPT=%HERE%webui.py"
if exist "%SCRIPT%" goto found
set "SCRIPT=%HERE%m\webui.py"
if exist "%SCRIPT%" goto found
set "SCRIPT=%HERE%..\m\webui.py"
if exist "%SCRIPT%" goto found
set "SCRIPT=%USERPROFILE%\Desktop\m\webui.py"
if exist "%SCRIPT%" goto found
echo [x] webui.py not found - put this file in the m\ folder or next to it
pause
exit /b 1

:found
set "PY=python"
where python >nul 2>&1
if errorlevel 1 set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
set "PYTHONIOENCODING=utf-8"
if not defined WEBUI_PORT set "WEBUI_PORT=8787"
if not defined GATE_PORT  set "GATE_PORT=20130"
if not defined WEBUI_GATE set "WEBUI_GATE=1"
if not defined WEBUI_VPN  set "WEBUI_VPN=0"
if not defined GATE_KEYPOOL   set "GATE_KEYPOOL=1"
if not defined GATE_POOL_PREFIX set "GATE_POOL_PREFIX=a"
if not defined GATE_POOL_UP   set "GATE_POOL_UP=https://tokenharbor.ai"
set "GATE_POOL_V6=0"
if not defined GATE_POOL_TRIES set "GATE_POOL_TRIES=150"
if not defined VPN_REGIONS set "VPN_REGIONS=usa"

if exist "%HERE%gate_boot.py" "%PY%" "%HERE%gate_boot.py"

if exist "%HERE%guard.bat" call "%HERE%guard.bat"

powershell -NoProfile -Command "try { Invoke-WebRequest -Uri 'http://127.0.0.1:%WEBUI_PORT%/api/state' -UseBasicParsing -TimeoutSec 3 | Out-Null; exit 0 } catch { exit 1 }"
if not errorlevel 1 (
  echo.
  echo  [i] panel already running at http://127.0.0.1:%WEBUI_PORT% - opening it
  echo      ^(close this window; the running panel keeps serving^)
  start "" "http://127.0.0.1:%WEBUI_PORT%"
  timeout /t 2 >nul
  exit /b 0
)

powershell -NoProfile -Command "$t=(Get-Date).AddSeconds(-10); Get-CimInstance Win32_Process | Where-Object { $_.CreationDate -lt $t -and (($_.Name -eq 'cmd.exe' -and $_.CommandLine -match 'web\.bat') -or ($_.Name -eq 'python.exe' -and $_.CommandLine -match 'webui\.py|nine_vpn\.py|foxserve\.py')) } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1

powershell -NoProfile -Command "Get-NetTCPConnection -State Listen -LocalPort %WEBUI_PORT% -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }" >nul 2>&1

if exist "%HERE%gate_boot.py" "%PY%" "%HERE%gate_boot.py"

echo.
echo  ============================================
echo    Token Harbor panel
echo  ============================================
echo    dashboard : http://127.0.0.1:%WEBUI_PORT%
echo    gateway   : http://127.0.0.1:%GATE_PORT%/v1
if "%WEBUI_VPN%"=="1" (echo    IP rotate : ON  - the gateway rotates the IP on 429) else (echo    IP rotate : off - enable it in the "Gateway / IP" tab)
echo.
echo    opening the panel... (close this window to stop)
echo.

start "" /min cmd /c "timeout /t 3 >nul ^& start "" http://127.0.0.1:%WEBUI_PORT%"

:run
"%PY%" -u "%SCRIPT%"
if errorlevel 1 (
  echo.
  echo [!] panel exited unexpectedly. Press Enter to restart, or close this window to quit.
  pause >nul
  goto run
)
endlocal
