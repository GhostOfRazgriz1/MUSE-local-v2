@echo off
echo Stopping MUSE services...

:: Kill backend (tree kill to catch uvicorn workers)
tasklist /fi "WINDOWTITLE eq MUSE - Backend" 2>nul | find "cmd.exe" >nul && (
    echo   Stopping backend...
    taskkill /fi "WINDOWTITLE eq MUSE - Backend" /t /f >nul 2>&1
)

:: Kill frontend
tasklist /fi "WINDOWTITLE eq MUSE - Frontend" 2>nul | find "cmd.exe" >nul && (
    echo   Stopping frontend...
    taskkill /fi "WINDOWTITLE eq MUSE - Frontend" /t /f >nul 2>&1
)

:: Kill any orphaned uvicorn/python processes from .venv
for /f "tokens=2" %%p in ('wmic process where "commandline like '%%agent_os%%.venv%%' and name='python.exe'" get processid 2^>nul ^| findstr /r "[0-9]"') do (
    echo   Killing orphaned worker PID %%p
    taskkill /pid %%p /f >nul 2>&1
)

echo Done.
