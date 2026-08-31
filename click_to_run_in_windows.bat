@echo off
setlocal enabledelayedexpansion
title Attendify Enhanced - System Setup
color 0b

set "VENV_DIR=%~dp0.venv"
set "PIP_TIMEOUT=120"
set "MAX_RETRIES=3"

:: Prevent Flask reloader from triggering duplicate batch executions
set FLASK_ENV=production

:: Detect Python executable
where python >nul 2>&1
if %errorlevel% equ 0 (
    set "PYTHON_PATH=python"
    goto :python_ok
)
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PYTHON_PATH=%LOCALAPPDATA%\Programs\Python\Python312\python.exe" & goto :python_ok
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PYTHON_PATH=%LOCALAPPDATA%\Programs\Python\Python313\python.exe" & goto :python_ok
if exist "C:\Python312\python.exe" set "PYTHON_PATH=C:\Python312\python.exe" & goto :python_ok
if exist "C:\Python313\python.exe" set "PYTHON_PATH=C:\Python313\python.exe" & goto :python_ok

color 0c
echo [!] CRITICAL ERROR: Python is not detected on your system or in PATH.
echo Please install Python 3.12 or 3.13 and add it to your PATH environment variable.
pause
exit /b

:python_ok
cls
echo.
echo  =======================================================================
echo                   ATTENDIFY :: SYSTEM INITIALIZATION
echo  =======================================================================
echo.

:: STEP 1: Check Virtual Environment
call :run_step 10 "Checking Virtual Environment..."
if not exist "%VENV_DIR%\Scripts\python.exe" (
    call :run_step 15 "Creating Python Virtual Environment..."
    "%PYTHON_PATH%" -m venv "%VENV_DIR%" >nul 2>&1
    if errorlevel 1 goto :error
)

:: STEP 2: Upgrade Pip & Build Tools
call :run_step 30 "Upgrading pip package manager..."
"%VENV_DIR%\Scripts\python.exe" -m pip install --upgrade pip >nul 2>&1
if errorlevel 1 goto :error

call :run_step 40 "Installing C++ Build Tools (cmake, setuptools)..."
"%VENV_DIR%\Scripts\python.exe" -m pip install cmake setuptools >nul 2>&1
if errorlevel 1 goto :error

:: STEP 3: Web & Database Stack
call :run_step 55 "Installing Web modules (Flask, OpenCV, NumPy)..."
"%VENV_DIR%\Scripts\python.exe" -m pip install --timeout %PIP_TIMEOUT% --retries %MAX_RETRIES% --prefer-binary numpy opencv-python flask bcrypt >nul 2>&1
if errorlevel 1 goto :error

:: STEP 4: AI Engine (PyTorch) - LONGEST STEP
call :run_step 75 "Downloading PyTorch Engine (~200MB - this will take time longer)..."
"%VENV_DIR%\Scripts\python.exe" -m pip install --timeout %PIP_TIMEOUT% --retries %MAX_RETRIES% --prefer-binary torch torchvision --index-url https://download.pytorch.org/whl/cpu >nul 2>&1
if errorlevel 1 goto :error

call :run_step 85 "Installing FaceNet Neural Network..."
"%VENV_DIR%\Scripts\python.exe" -m pip install --timeout %PIP_TIMEOUT% --retries %MAX_RETRIES% --prefer-binary facenet-pytorch --no-deps >nul 2>&1
if errorlevel 1 goto :error

:: STEP 5: Supporting Data Libraries
call :run_step 95 "Installing Data and Reporting Libraries..."
"%VENV_DIR%\Scripts\python.exe" -m pip install --timeout %PIP_TIMEOUT% --retries %MAX_RETRIES% --prefer-binary requests tqdm scipy scikit-learn fpdf2 >nul 2>&1
if errorlevel 1 goto :error

:: Completion
call :run_step 100 "System Setup Complete!"
echo.
echo.
echo  =======================================================================
echo                      LAUNCHING ATTENDIFY DASHBOARD
echo  =======================================================================
echo.

if not exist "%~dp0app.py" (
    color 0c
    echo  [!] ERROR: app.py was not found in %~dp0
    pause
    exit /b
)

:: Appended Passive Loading Animation before app initialization
call :start_spinner

"%VENV_DIR%\Scripts\python.exe" app.py
if errorlevel 1 goto :error
exit /b

:: -------------------------------------------------------------------------
:: UI STEP DISPLAY ROUTINE
:: -------------------------------------------------------------------------
:run_step
setlocal
set /a pct=%~1
set "label=%~2"

set /a filled=pct * 30 / 100
set /a empty=30 - filled

set "bar="
for /l %%i in (1,1,%filled%) do set "bar=!bar!#"
for /l %%i in (1,1,%empty%) do set "bar=!bar!-"

echo  [* %pct%%%] [!bar!] - %label%
endlocal
goto :eof

:: -------------------------------------------------------------------------
:: PASSIVE SPINNER ANIMATION (Appended at the bottom)
:: -------------------------------------------------------------------------
:start_spinner
setlocal
echo|set /p="  [  ] Initializing server environment..."
for /l %%i in (1,1,3) do (
    <nul set /p="[1D\" & ping -n 1 127.0.0.1 >nul
    <nul set /p="[1D|" & ping -n 1 127.0.0.1 >nul
    <nul set /p="[1D/" & ping -n 1 127.0.0.1 >nul
    <nul set /p="[1D-" & ping -n 1 127.0.0.1 >nul
)
<nul set /p="[1D[K[OK] Starting server... "
echo.
echo.
endlocal
goto :eof

:error
color 0c
echo.
echo  [!] CRITICAL ERROR: Setup failed during installation.
echo  Please check your internet connection or inspect setup logs.
pause
exit /b