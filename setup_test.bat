@echo off
setlocal enabledelayedexpansion

echo ================================================
echo   Test Setup (no scheduler, no auto-run)
echo ================================================
echo.

set "PROJECT_DIR=%~dp0"
set "PROJECT_DIR=%PROJECT_DIR:~0,-1%"

echo [1/4] Checking Python...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo      [ERROR] Python not found. Install Python 3.11+ first.
    pause
    exit /b 1
)
echo      OK

echo.
echo [2/4] Installing Python packages...
cd /d "%PROJECT_DIR%"
python -m pip install --upgrade pip --quiet 2>nul
python -m pip install -r requirements.txt --quiet 2>nul
python -m pip install python-dotenv --quiet 2>nul
echo      OK

echo.
echo [3/4] Checking .env...
if not exist "%PROJECT_DIR%\.env" (
    echo.
    echo      .env file not found. Creating it now...
    echo.
    set /p "API_KEY=      Enter DATA_GO_KR API Key: "
    set /p "SHEET_ID=      Enter GOOGLE_SHEET_ID (empty to skip): "
    set /p "GOOG_CRED=      Enter GOOGLE_CREDENTIALS_JSON path (empty to skip): "
    (
        echo DATA_GO_KR_API_KEY=!API_KEY!
        echo GOOGLE_SHEET_ID=!SHEET_ID!
        echo GOOGLE_CREDENTIALS_JSON=!GOOG_CRED!
    ) > "%PROJECT_DIR%\.env"
    echo.
    echo      .env created
)
echo      OK

echo.
echo [4/4] Ready!
echo.
echo ================================================
echo   Test Setup Complete (no scheduler registered)
echo ================================================
echo.
echo   Test commands:
echo     python crawl.py --test 3
echo     python monitor.py --days 7 --no-upload
echo.
echo   When ready for production, run setup_windows.bat
echo   on the work PC (as Administrator) to register schedules.
echo.
pause
