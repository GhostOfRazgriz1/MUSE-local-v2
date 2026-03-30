@echo off
setlocal enabledelayedexpansion

title MUSE - Startup
color 0A

echo.
echo  ============================================
echo   MUSE - One-Click Startup
echo  ============================================
echo.

set "ROOT=%~dp0"
cd /d "%ROOT%"

:: -----------------------------------------------
:: 1. Check Python venv
:: -----------------------------------------------
echo [1/4] Checking Python virtual environment...
if not exist ".venv\Scripts\python.exe" (
    echo   Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo ERROR: Failed to create venv. Is Python 3.12+ installed?
        pause
        exit /b 1
    )
)
set "PYTHON=%ROOT%.venv\Scripts\python.exe"
set "PIP=%ROOT%.venv\Scripts\pip.exe"
echo   OK - venv at .venv\

:: -----------------------------------------------
:: 2. Install Python dependencies
:: -----------------------------------------------
echo.
echo [2/4] Installing Python dependencies...
"%PIP%" install --quiet --upgrade pip >nul 2>&1

:: Install main package in editable mode
"%PIP%" install -e . --quiet 2>&1 | findstr /V "already satisfied"
if errorlevel 1 (
    echo   Installing dependencies - this may take a minute on first run...
    "%PIP%" install -e .
)

:: Install the SDK
"%PIP%" install -e sdk --quiet 2>&1 | findstr /V "already satisfied"

:: Check critical deps
"%PYTHON%" -c "import fastapi, aiosqlite, httpx, pydantic; print('  OK - all core dependencies installed')" 2>&1
if errorlevel 1 (
    echo   ERROR: Some dependencies failed to install. Check output above.
    pause
    exit /b 1
)

:: -----------------------------------------------
:: 3. Install frontend dependencies
:: -----------------------------------------------
echo.
echo [3/4] Setting up frontend...
cd /d "%ROOT%frontend"
if not exist "node_modules" (
    echo   Installing npm packages on first run...
    call npm install 2>&1
) else (
    echo   OK - node_modules exists
)
cd /d "%ROOT%"

:: -----------------------------------------------
:: 4. Start services
:: -----------------------------------------------
echo.
echo [4/4] Starting MUSE...

echo.
echo  ============================================
echo   Backend:  http://127.0.0.1:8080
echo   Frontend: http://127.0.0.1:3000
echo   API Docs: http://127.0.0.1:8080/docs
echo  ============================================
echo.

:: Write backend launcher
> "%ROOT%_run_backend.cmd" (
    echo @echo off
    echo title MUSE - Backend
    echo cd /d "%ROOT%"
    echo "%PYTHON%" -m uvicorn muse.api.app:create_app --factory --host 127.0.0.1 --port 8080 --reload --app-dir src
    echo pause
)

:: Write frontend launcher
> "%ROOT%_run_frontend.cmd" (
    echo @echo off
    echo title MUSE - Frontend
    echo cd /d "%ROOT%frontend"
    echo npx vite --host 127.0.0.1 --port 3000
    echo pause
)

:: Start backend
start "MUSE - Backend" cmd /c "%ROOT%_run_backend.cmd"

:: Give backend a moment
timeout /t 4 /nobreak >nul

:: Start frontend
start "MUSE - Frontend" cmd /c "%ROOT%_run_frontend.cmd"

:: Wait then open browser
timeout /t 4 /nobreak >nul
echo   Opening browser...
start http://127.0.0.1:3000

echo.
echo   MUSE is running!
echo   Configure your API keys in Settings if this is your first time.
echo   Close this window or press any key to exit - services keep running.
echo.
pause
