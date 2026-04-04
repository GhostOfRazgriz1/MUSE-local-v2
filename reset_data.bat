@echo off
echo === MUSE Data Reset ===
echo.

:: Use project venv if available (has keyring installed)
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" "%~dp0reset_data.py" %*
) else (
    python "%~dp0reset_data.py" %*
)
pause
