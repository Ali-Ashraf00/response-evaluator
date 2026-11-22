@echo off
cd /d "%~dp0"

set "PY_EXE=C:\Users\rocka\AppData\Local\Programs\Python\Python314\python.exe"
if not exist "%PY_EXE%" set "PY_EXE=python"

echo ------------------------------------------------------------
echo Checking for previous running server on port 8000...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8000" ^| findstr "LISTENING"') do (
    echo Stopping old running process PID %%a...
    taskkill /F /PID %%a >nul 2>&1
)

echo ========================================================
echo   LLM Eval Platform (Vindex 1-6 Standard)
echo   Starting server at http://127.0.0.1:8000
echo   Press Ctrl+C in this window to stop.
echo ========================================================

"%PY_EXE%" "%~dp0server.py" --open
pause
