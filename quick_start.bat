@echo off
setlocal enabledelayedexpansion

echo ================================================
echo   Quick Start - Clone + Test Setup
echo ================================================
echo.

set "PROJECT_DIR=%USERPROFILE%\crawl-project"

:: Check git
git --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Git not found. Install Git for Windows first.
    pause
    exit /b 1
)

:: Clone or pull
if exist "%PROJECT_DIR%\monitor.py" (
    echo [1/3] Already exists. Pulling latest...
    cd /d "%PROJECT_DIR%"
    git pull origin claude/git-windows-setup-E0Sa2
) else (
    echo [1/3] Cloning project...
    if exist "%PROJECT_DIR%" rmdir /s /q "%PROJECT_DIR%"
    git clone -b claude/git-windows-setup-E0Sa2 https://github.com/kangdotsukha-oss/crawl-project.git "%PROJECT_DIR%"
    cd /d "%PROJECT_DIR%"
)
echo      OK

:: Python packages
echo.
echo [2/3] Installing packages...
python -m pip install --upgrade pip --quiet 2>nul
python -m pip install -r requirements.txt --quiet 2>nul
python -m pip install python-dotenv --quiet 2>nul
echo      OK

:: .env
echo.
echo [3/3] Setting up .env...
if not exist "%PROJECT_DIR%\.env" (
    echo.
    echo      API Key is needed from data.go.kr
    echo      (Leave empty to skip for now)
    echo.
    set /p "API_KEY=      DATA_GO_KR API Key: "
    set /p "SHEET_ID=      GOOGLE_SHEET_ID: "
    set /p "GOOG_CRED=      GOOGLE_CREDENTIALS_JSON: "
    (
        echo DATA_GO_KR_API_KEY=!API_KEY!
        echo GOOGLE_SHEET_ID=!SHEET_ID!
        echo GOOGLE_CREDENTIALS_JSON=!GOOG_CRED!
    ) > "%PROJECT_DIR%\.env"
    echo      .env created
) else (
    echo      .env exists
)

echo.
echo ================================================
echo   Done! Project at: %PROJECT_DIR%
echo ================================================
echo.
echo   cd %PROJECT_DIR%
echo   python monitor.py --days 7 --no-upload
echo.
cd /d "%PROJECT_DIR%"
pause
