@echo off
rem Overlay client launcher. Double click, or: run_client.bat --img tests\images\board_ingame.png
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

set "PY=C:\Users\22482.BD\miniconda3\envs\jcc_robot\python.exe"
if not exist "%PY%" (
    echo [!] python not found: %PY%
    echo     Edit PY in this file, or use an activated env's python.
    pause
    exit /b 1
)

"%PY%" -X utf8 -u -m scripts.overlay_client %*
set RC=%ERRORLEVEL%
if not "%RC%"=="0" (
    echo.
    echo [X] exit code %RC%
    pause
)
exit /b %RC%