@echo off
setlocal EnableDelayedExpansion
title Echo Launcher
color 0B

:: Set up a fancy header
echo.
echo  =============================================
echo        ECHO LAUNCHER - MINECRAFT CLIENT      
echo  =============================================
echo.

:: Check if Python is installed
python --version > nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not in PATH.
    echo Please install Python 3.7 or higher from https://python.org
    echo.
    echo Press any key to exit...
    pause > nul
    exit /b 1
)

:: Check if sources directory exists, create if it doesn't
if not exist sources (
    echo [INFO] Creating sources directory...
    mkdir sources
    mkdir sources\versions
    mkdir sources\assets
    mkdir sources\libraries
)

:: Check if requirements.txt exists
if not exist requirements.txt (
    echo [ERROR] requirements.txt not found!
    echo.
    echo Press any key to exit...
    pause > nul
    exit /b 1
)

echo [INFO] Installing dependencies...
pip install -r requirements.txt

if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Failed to install dependencies.
    echo.
    echo Press any key to exit...
    pause > nul
    exit /b 1
)

echo.
echo [INFO] Starting Echo Launcher...
echo.

:: Run the launcher
python launcher.py

if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Echo Launcher exited with an error.
    echo.
)

echo.
echo Thank you for using Echo Launcher!
echo Press any key to exit...
pause > nul 