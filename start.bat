@echo off
cd /d "%~dp0"

echo ======================================
echo   Playlist Maker
echo ======================================
echo.

REM ── 1. Find Python ────────────────────────────────────────────────────────
REM Try "python" first, then "py" (the Windows Python Launcher)
python --version >nul 2>&1
if not errorlevel 1 (
    set PYTHON=python
    goto found_python
)
py --version >nul 2>&1
if not errorlevel 1 (
    set PYTHON=py
    goto found_python
)

echo ERROR: Python 3 is not installed or not on your PATH.
echo.
echo   Download it from: https://www.python.org/downloads/windows/
echo   During install, check "Add python.exe to PATH".
echo.
pause
exit /b 1

:found_python
for /f "tokens=*" %%v in ('%PYTHON% --version 2^>^&1') do echo %%v found.

REM ── 2. Create virtual environment if needed ───────────────────────────────
if not exist "venv\" (
    echo.
    echo Setting up virtual environment (first run only)...
    %PYTHON% -m venv venv
    if errorlevel 1 (
        echo.
        echo ERROR: Could not create virtual environment.
        pause
        exit /b 1
    )
    echo Virtual environment created.
)

REM ── 3. Activate the virtual environment ──────────────────────────────────
call venv\Scripts\activate.bat
if errorlevel 1 (
    echo.
    echo ERROR: Could not activate virtual environment.
    pause
    exit /b 1
)

REM ── 4. Upgrade pip and install dependencies ───────────────────────────────
echo.
echo Checking dependencies...
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
if errorlevel 1 (
    echo.
    echo ERROR: Could not install dependencies.
    echo Check your internet connection and try again.
    pause
    exit /b 1
)
echo Dependencies ready.

REM ── 5. Launch the web app ─────────────────────────────────────────────────
echo.
echo Starting Playlist Maker...
echo.
python web.py

REM Keep the window open if something went wrong
if errorlevel 1 (
    echo.
    echo The app stopped with an error.
    echo Scroll up to read what went wrong.
    pause
)
