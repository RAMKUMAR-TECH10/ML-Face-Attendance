@echo off
setlocal enabledelayedexpansion
title Attendify Enhanced - System Setup
color 0b

set "LOG_FILE=%~dp0setup_log.txt"
set "VENV_DIR=%~dp0.venv"
set "PIP_TIMEOUT=120"
set "MAX_RETRIES=3"

:: Find python executable
where python >nul 2>&1
if %errorlevel% equ 0 (
    set "PYTHON_PATH=python"
    goto :python_ok
)

:: Try common install locations
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    set "PYTHON_PATH=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    goto :python_ok
)
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" (
    set "PYTHON_PATH=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    goto :python_ok
)
if exist "C:\Python312\python.exe" (
    set "PYTHON_PATH=C:\Python312\python.exe"
    goto :python_ok
)
if exist "C:\Python313\python.exe" (
    set "PYTHON_PATH=C:\Python313\python.exe"
    goto :python_ok
)

color 0c
echo [!] CRITICAL ERROR: Python is not detected on your system or in the PATH.
echo Please install Python 3.12 or 3.13 and add it to your PATH environment variable.
pause
exit /b

:python_ok
echo ===================================================
echo   ATTENDIFY: TOTAL SYSTEM INITIALIZATION
echo ===================================================
echo Logging details to: %LOG_FILE%
echo. > "%LOG_FILE%"

:: PHASE 1: VENV CREATION
echo [20%%] Checking Virtual Environment...
if exist "%VENV_DIR%\Scripts\python.exe" (
    echo [OK] Virtual Environment already exists.
    goto :phase2
)
echo       - Creating isolated environment...
"%PYTHON_PATH%" -m venv "%VENV_DIR%" >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    color 0c
    echo [!] CRITICAL ERROR: Failed to create virtual environment. 
    echo Check setup_log.txt for details.
    pause
    exit /b
)
echo [OK] Environment Created.

:phase2
:: PHASE 2: CORE TOOLS (20% - 40%)
echo [40%%] Installing Build Tools...
"%VENV_DIR%\Scripts\python.exe" -m pip install --upgrade pip >> "%LOG_FILE%" 2>&1
"%VENV_DIR%\Scripts\python.exe" -m pip install cmake setuptools >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    color 0c
    echo [!] CRITICAL ERROR: Failed to install build tools.
    echo Check setup_log.txt for details.
    pause
    exit /b
)

:: PHASE 3: WEB & DATABASE (40% - 70%)
echo [70%%] Syncing Web and Database Modules...
"%VENV_DIR%\Scripts\python.exe" -m pip install --timeout %PIP_TIMEOUT% --retries %MAX_RETRIES% --prefer-binary numpy opencv-python flask bcrypt >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    color 0c
    echo [!] CRITICAL ERROR: Failed to install web/database modules.
    echo Check setup_log.txt for details.
    pause
    exit /b
)

:: PHASE 4: AI ENGINE - FACENET (70% - 100%)
echo [90%%] Finalizing AI Core (FaceNet + MTCNN)...
echo       - Installing PyTorch (CPU-optimized)...
"%VENV_DIR%\Scripts\python.exe" -m pip install --timeout %PIP_TIMEOUT% --retries %MAX_RETRIES% --prefer-binary torch torchvision --index-url https://download.pytorch.org/whl/cpu >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    color 0c
    echo [!] CRITICAL ERROR: Failed to install PyTorch.
    echo Check setup_log.txt for details.
    pause
    exit /b
)

echo       - Installing FaceNet-PyTorch...
"%VENV_DIR%\Scripts\python.exe" -m pip install --timeout %PIP_TIMEOUT% --retries %MAX_RETRIES% --prefer-binary facenet-pytorch --no-deps >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    color 0c
    echo [!] CRITICAL ERROR: Failed to install facenet-pytorch.
    echo Check setup_log.txt for details.
    pause
    exit /b
)

echo       - Installing supporting libraries...
"%VENV_DIR%\Scripts\python.exe" -m pip install --timeout %PIP_TIMEOUT% --retries %MAX_RETRIES% --prefer-binary requests tqdm scipy scikit-learn >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    color 0c
    echo [!] CRITICAL ERROR: Failed to install supporting libraries.
    echo Check setup_log.txt for details.
    pause
    exit /b
)

echo [100%%] ALL SYSTEMS OPERATIONAL.
echo ===================================================
echo   LAUNCHING ATTENDIFY DASHBOARD...
echo ===================================================
"%VENV_DIR%\Scripts\python.exe" app.py

if errorlevel 1 (
    color 0c
    echo [!] CRITICAL ERROR: System failed to launch or exited with error.
    echo Please check the terminal above for Python traceback / errors.
)
pause
