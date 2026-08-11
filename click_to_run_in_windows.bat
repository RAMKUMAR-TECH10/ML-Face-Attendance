@echo off
setlocal enabledelayedexpansion
title Attendify Enhanced - System Setup
color 0b

set "LOG_FILE=%~dp0setup_log.txt"
set "VENV_DIR=%~dp0.venv"

:: Find python executable
where python >nul 2>&1
if %errorlevel% equ 0 (
    set "PYTHON_PATH=python"
    goto :python_ok
)

:: Try default install locations as fallback
if exist "C:\Users\sutha\AppData\Local\Programs\Python\Python312\python.exe" (
    set "PYTHON_PATH=C:\Users\sutha\AppData\Local\Programs\Python\Python312\python.exe"
    goto :python_ok
)
if exist "C:\Users\sutha\AppData\Local\Programs\Python\Python313\python.exe" (
    set "PYTHON_PATH=C:\Users\sutha\AppData\Local\Programs\Python\Python313\python.exe"
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
echo [40%%] Installing Build Tools (CMake / setuptools)...
"%VENV_DIR%\Scripts\python.exe" -m pip install --upgrade pip >> "%LOG_FILE%" 2>&1
"%VENV_DIR%\Scripts\python.exe" -m pip install cmake setuptools==81.0.0 >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    color 0c
    echo [!] CRITICAL ERROR: Failed to install build tools [CMake / setuptools].
    echo Check setup_log.txt for details.
    pause
    exit /b
)

:: PHASE 3: WEB & DATABASE (40% - 70%)
echo [70%%] Syncing Web and Database Modules...
"%VENV_DIR%\Scripts\python.exe" -m pip install numpy opencv-python flask bcrypt >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    color 0c
    echo [!] CRITICAL ERROR: Failed to install modules [numpy/opencv/flask/bcrypt].
    echo Check setup_log.txt for details.
    pause
    exit /b
)

:: PHASE 4: AI ENGINE (70% - 100%)
echo [90%%] Finalizing AI Core (face_recognition)...
echo       - NOTE: This part is heavy, please wait...

:: Detect Python version of venv to fetch precompiled dlib wheel (safely handle spaces in path)
"%VENV_DIR%\Scripts\python.exe" -V > "%VENV_DIR%\pyver.txt" 2>&1
for /f "usebackq tokens=2 delims= " %%i in ("%VENV_DIR%\pyver.txt") do set "PY_FULL_VERSION=%%i"
for /f "tokens=2 delims=." %%b in ("%PY_FULL_VERSION%") do set "PY_MINOR=%%b"
del "%VENV_DIR%\pyver.txt"

echo       - Detected Python 3.!PY_MINOR! in Virtual Environment.
echo       - Installing pre-compiled dlib binaries for Windows...

set "DLIB_WHL="
if "!PY_MINOR!"=="10" set "DLIB_WHL=https://github.com/z-mahmud22/Dlib_Windows_Python3.x/raw/main/dlib-19.22.99-cp310-cp310-win_amd64.whl"
if "!PY_MINOR!"=="11" set "DLIB_WHL=https://github.com/z-mahmud22/Dlib_Windows_Python3.x/raw/main/dlib-19.24.1-cp311-cp311-win_amd64.whl"
if "!PY_MINOR!"=="12" set "DLIB_WHL=https://github.com/z-mahmud22/Dlib_Windows_Python3.x/raw/main/dlib-19.24.99-cp312-cp312-win_amd64.whl"
if "!PY_MINOR!"=="13" set "DLIB_WHL=https://github.com/z-mahmud22/Dlib_Windows_Python3.x/releases/download/v1/dlib-20.0.99-cp313-cp313-win_amd64.whl"
if not defined DLIB_WHL set "DLIB_WHL=dlib"

"%VENV_DIR%\Scripts\python.exe" -m pip install !DLIB_WHL! >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    color 0c
    echo [!] CRITICAL ERROR: Failed to install dlib wheel for Python 3.!PY_MINOR!.
    echo Check setup_log.txt for details.
    pause
    exit /b
)

"%VENV_DIR%\Scripts\python.exe" -m pip install face_recognition >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    color 0c
    echo [!] CRITICAL ERROR: Failed to install face_recognition.
    echo Check setup_log.txt for details.
    pause
    exit /b
)

:: Reinstalling models if necessary
"%VENV_DIR%\Scripts\python.exe" -m pip install --force-reinstall https://github.com/ageitgey/face_recognition_models/archive/master.zip >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    color 0c
    echo [!] CRITICAL ERROR: Failed to install face_recognition_models database.
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