@echo off
title CS2 Server Creator

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Run setup.bat first.
    pause
    exit /b 1
)

python main.py %*
pause
