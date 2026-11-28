@echo off
setlocal EnableExtensions
set "HERE=%~dp0"
powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath 'cmd.exe' -ArgumentList '/k','\"%HERE%split_tunnel.bat\" run'"
exit /b 0

:run
set "PY=python"
where python >nul 2>&1
if errorlevel 1 set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
set "PYTHONIOENCODING=utf-8"
echo === bat + bypass app nang (chua python/camoufox) ===
"%PY%" "%HERE%split_tunnel.py" auto
echo.
echo === trang thai ===
"%PY%" "%HERE%split_tunnel.py" status
echo.
pause
endlocal
