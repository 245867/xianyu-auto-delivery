@echo off
setlocal EnableExtensions

cd /d "%~dp0"
set "PROJECT_ROOT=%~dp0"
set "PYTHON_EXE=%PROJECT_ROOT%.venv\Scripts\python.exe"

echo [1/4] Checking Python environment...
if not exist "%PYTHON_EXE%" (
    where py >nul 2>&1
    if not errorlevel 1 (
        py -3.11 -m venv "%PROJECT_ROOT%.venv"
    ) else (
        where python >nul 2>&1
        if errorlevel 1 (
            echo Python 3.11 or newer was not found.
            pause
            exit /b 1
        )
        python -m venv "%PROJECT_ROOT%.venv"
    )
)

if not exist "%PYTHON_EXE%" (
    echo Failed to create the virtual environment.
    pause
    exit /b 1
)

echo [2/4] Checking Python dependencies...
"%PYTHON_EXE%" -c "import fastapi, uvicorn, playwright" >nul 2>&1
if errorlevel 1 (
    "%PYTHON_EXE%" -m pip install --upgrade pip
    "%PYTHON_EXE%" -m pip install -r "%PROJECT_ROOT%requirements.txt"
    if errorlevel 1 (
        echo Python dependency installation failed.
        pause
        exit /b 1
    )
)

echo [3/4] Checking Playwright Chromium...
"%PYTHON_EXE%" -m playwright install chromium

if not exist "%PROJECT_ROOT%static\index.html" (
    echo [4/4] Building frontend...
    where npm >nul 2>&1
    if errorlevel 1 (
        echo npm was not found. Install Node.js 20 or newer first.
        pause
        exit /b 1
    )
    pushd "%PROJECT_ROOT%frontend"
    call npm install
    if not errorlevel 1 call npm run build
    set "FRONTEND_EXIT=%ERRORLEVEL%"
    popd
    if not "%FRONTEND_EXIT%"=="0" (
        echo Frontend build failed.
        pause
        exit /b 1
    )
) else (
    echo [4/4] Frontend build already exists.
)

echo.
echo Service URL: http://localhost:8080/
echo Press Ctrl+C in this window to stop the service.
start "" powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Sleep -Seconds 5; Start-Process 'http://localhost:8080/'"
"%PYTHON_EXE%" "%PROJECT_ROOT%Start.py"
set "START_EXIT=%ERRORLEVEL%"
pause
exit /b %START_EXIT%
