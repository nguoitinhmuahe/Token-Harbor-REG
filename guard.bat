@echo off
setlocal EnableExtensions
set "HERE=%~dp0"
set "PY=python"
set "PYW=pythonw"
where python >nul 2>&1
if errorlevel 1 set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
where pythonw >nul 2>&1
if errorlevel 1 set "PYW=%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe"
if not exist "%PYW%" set "PYW=%PY%"
set "PYTHONIOENCODING=utf-8"
start "" /min "%PYW%" "%HERE%guard.py"
exit /b 0
