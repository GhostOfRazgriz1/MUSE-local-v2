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

:: HuggingFace mirror is auto-detected. To force it manually:
:: set "HF_ENDPOINT=https://hf-mirror.com"

:: Use %TEMP% for launcher scripts to avoid OneDrive locks and
:: "Permission denied" when re-running while services are still alive.
set "LAUNCHER_DIR=%TEMP%\muse_launchers"
if not exist "!LAUNCHER_DIR!" mkdir "!LAUNCHER_DIR!"

:: Kill any leftover MUSE service windows from a previous run
tasklist /fi "WINDOWTITLE eq MUSE - Backend" 2>nul | find "cmd.exe" >nul && (
    echo   Stopping previous backend...
    taskkill /fi "WINDOWTITLE eq MUSE - Backend" /f >nul 2>&1
    timeout /t 1 /nobreak >nul
)
tasklist /fi "WINDOWTITLE eq MUSE - Frontend" 2>nul | find "cmd.exe" >nul && (
    echo   Stopping previous frontend...
    taskkill /fi "WINDOWTITLE eq MUSE - Frontend" /f >nul 2>&1
    timeout /t 1 /nobreak >nul
)

:: -----------------------------------------------
:: 1. Check Python venv
:: -----------------------------------------------
echo [1/5] Checking Python virtual environment...
if not exist ".venv\Scripts\python.exe" (
    echo   Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo ERROR: Failed to create venv. Is Python 3.10+ installed?
        pause
        exit /b 1
    )
)
set "PYTHON=%ROOT%.venv\Scripts\python.exe"
set "PIP=%ROOT%.venv\Scripts\pip.exe"
echo   OK - venv at .venv\

:: -----------------------------------------------
:: 2. Install Python dependencies (skip if unchanged)
:: -----------------------------------------------
echo.
echo [2/5] Checking Python dependencies...

set "STAMP_FILE=%ROOT%.venv\.dep_stamp"
set "NEEDS_INSTALL=0"

for /f "delims=" %%H in ('certutil -hashfile "%ROOT%pyproject.toml" SHA256 2^>nul ^| findstr /r "^[0-9a-f]"') do set "HASH1=%%H"
for /f "delims=" %%H in ('certutil -hashfile "%ROOT%sdk\pyproject.toml" SHA256 2^>nul ^| findstr /r "^[0-9a-f]"') do set "HASH2=%%H"
set "DEP_HASH=!HASH1!+!HASH2!"

if not exist "!STAMP_FILE!" (
    set "NEEDS_INSTALL=1"
) else (
    set /p SAVED_HASH=<"!STAMP_FILE!"
    if not "!SAVED_HASH!"=="!DEP_HASH!" set "NEEDS_INSTALL=1"
)

if "!NEEDS_INSTALL!"=="1" (
    echo   Dependencies changed - installing...
    "%PIP%" install --quiet --upgrade pip >nul 2>&1
    "%PIP%" install -e . --quiet >nul 2>&1
    "%PIP%" install -e sdk --quiet >nul 2>&1

    "%PYTHON%" -c "import fastapi, aiosqlite, httpx, pydantic" >nul 2>&1
    if errorlevel 1 (
        echo   ERROR: Some dependencies failed to install. Retrying with verbose output...
        "%PIP%" install -e .
        "%PIP%" install -e sdk
        "%PYTHON%" -c "import fastapi, aiosqlite, httpx, pydantic" >nul 2>&1
        if errorlevel 1 (
            echo   ERROR: Dependencies still broken. Check output above.
            pause
            exit /b 1
        )
    )

    echo !DEP_HASH!> "!STAMP_FILE!"
    echo   OK - dependencies installed
) else (
    echo   OK - dependencies up to date ^(skipped install^)
)

:: -----------------------------------------------
:: 3. Install frontend dependencies (skip if unchanged)
:: -----------------------------------------------
echo.
echo [3/5] Setting up frontend...

where npm >nul 2>&1
if errorlevel 1 (
    echo   WARNING: npm not found - frontend will not be available.
    echo   Install Node.js 18+ from https://nodejs.org/
    set "SKIP_FRONTEND=1"
    goto :preflight
)

:: Check Node.js version
for /f "tokens=1 delims=v" %%V in ('node --version 2^>nul') do set "NODE_VER=%%V"
for /f "tokens=1 delims=." %%M in ("!NODE_VER!") do set "NODE_MAJOR=%%M"
if !NODE_MAJOR! lss 18 (
    echo   WARNING: Node.js v!NODE_VER! is too old. Need 18+.
    echo   Download from https://nodejs.org/
    set "SKIP_FRONTEND=1"
    goto :preflight
)
echo   OK - Node.js v!NODE_VER!

set "SKIP_FRONTEND=0"
set "NPM_STAMP=%ROOT%frontend\node_modules\.pkg_stamp"

for /f "delims=" %%H in ('certutil -hashfile "%ROOT%frontend\package.json" SHA256 2^>nul ^| findstr /r "^[0-9a-f]"') do set "NPM_HASH=%%H"

set "NEEDS_NPM=0"
if not exist "%ROOT%frontend\node_modules" (
    set "NEEDS_NPM=1"
) else if not exist "!NPM_STAMP!" (
    set "NEEDS_NPM=1"
) else (
    set /p SAVED_NPM=<"!NPM_STAMP!"
    if not "!SAVED_NPM!"=="!NPM_HASH!" set "NEEDS_NPM=1"
)

if "!NEEDS_NPM!"=="1" (
    echo   Installing npm packages...
    cd /d "%ROOT%frontend"
    call npm install 2>&1
    echo !NPM_HASH!> "!NPM_STAMP!"
    cd /d "%ROOT%"
    echo   OK - npm packages installed
) else (
    echo   OK - node_modules up to date ^(skipped install^)
)

:: -----------------------------------------------
:: 4. Preflight checks
:: -----------------------------------------------
:preflight
echo.
echo [4/5] Running preflight checks...

"%PYTHON%" -m muse.preflight
if errorlevel 1 (
    echo.
    echo   Preflight failed. Fix the issues above before starting.
    pause
    exit /b 1
)

if "!SKIP_FRONTEND!"=="0" (
    echo   Checking frontend types...
    cd /d "%ROOT%frontend"
    call npx tsc --noEmit >nul 2>&1
    if errorlevel 1 (
        echo.
        echo   TypeScript errors found:
        call npx tsc --noEmit --pretty
        echo.
        echo   WARNING: Frontend may crash at runtime. Launching anyway...
    ) else (
        echo   OK - TypeScript clean
    )
    cd /d "%ROOT%"
)

:: -----------------------------------------------
:: 5. Start services
:: -----------------------------------------------
echo.
echo [5/5] Starting MUSE...

echo.
echo  ============================================
echo   Backend:  http://127.0.0.1:8080
if "!SKIP_FRONTEND!"=="0" echo   Frontend: http://127.0.0.1:3000
echo   API Docs: http://127.0.0.1:8080/docs
echo  ============================================
echo.

:: Write backend launcher (live output, stays open on crash)
> "!LAUNCHER_DIR!\_backend.cmd" (
    echo @echo off
    echo title MUSE - Backend
    echo "!PYTHON!" -m uvicorn muse.api.app:create_app --factory --host 127.0.0.1 --port 8080 --reload --app-dir "!ROOT!src" --reload-exclude ".venv" --reload-exclude "node_modules" --reload-exclude "frontend/test-results"
    echo if errorlevel 1 (
    echo     echo.
    echo     echo ===== Backend crashed =====
    echo     echo The error is above.
    echo     echo.
    echo     pause
    echo ^)
)

start "MUSE - Backend" "!LAUNCHER_DIR!\_backend.cmd"

:: Wait for backend to be ready (poll instead of fixed timeout)
echo   Waiting for backend...
set "RETRIES=0"
:wait_backend
set /a RETRIES+=1
if !RETRIES! gtr 30 (
    echo.
    echo   ERROR: Backend failed to start within 15s.
    echo   Check the "MUSE - Backend" window for errors.
    echo.
    goto :start_frontend
)
timeout /t 1 /nobreak >nul
powershell -Command "try { (Invoke-WebRequest -Uri http://127.0.0.1:8080/docs -UseBasicParsing -TimeoutSec 1).StatusCode } catch { exit 1 }" >nul 2>&1
if errorlevel 1 goto :wait_backend
echo   Backend ready.

:: Start frontend
:start_frontend
if "!SKIP_FRONTEND!"=="1" goto :done

> "!LAUNCHER_DIR!\_frontend.cmd" (
    echo @echo off
    echo title MUSE - Frontend
    echo cd /d "!ROOT!frontend"
    echo npx vite --host 127.0.0.1 --port 3000
    echo if errorlevel 1 (
    echo     echo.
    echo     echo ===== Frontend crashed =====
    echo     echo The error is above.
    echo     echo.
    echo     pause
    echo ^)
)

start "MUSE - Frontend" "!LAUNCHER_DIR!\_frontend.cmd"

:: Wait for frontend to be ready (poll)
echo   Waiting for frontend...
set "RETRIES=0"
:wait_frontend
set /a RETRIES+=1
if !RETRIES! gtr 20 (
    echo.
    echo   ERROR: Frontend failed to start within 10s.
    echo   Check the "MUSE - Frontend" window for errors.
    echo.
    goto :open_browser
)
timeout /t 1 /nobreak >nul
powershell -Command "try { (Invoke-WebRequest -Uri http://127.0.0.1:3000 -UseBasicParsing -TimeoutSec 1).StatusCode } catch { exit 1 }" >nul 2>&1
if errorlevel 1 goto :wait_frontend
echo   Frontend ready.

:open_browser
echo   Opening browser...
start http://127.0.0.1:3000

:done
echo.
echo   MUSE is running!
echo   Configure your API keys in Settings if this is your first time.
echo   Close this window or press any key to exit - services keep running.
echo.
pause
