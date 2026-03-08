@echo off
setlocal enabledelayedexpansion

echo ================================================
echo   Crawler Auto Setup Script (Windows)
echo ================================================
echo.

:: Check admin privileges
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Please run as Administrator.
    echo Right-click this file and select "Run as administrator"
    pause
    exit /b 1
)

:: ── 1. Check winget ────────────────────────────────────────────────────────
echo [1/6] Checking package manager...
winget --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] winget not found. Windows 10 1709 or later required.
    echo Please install "App Installer" from Microsoft Store.
    pause
    exit /b 1
)
echo      winget OK

:: ── 2. Install Python ──────────────────────────────────────────────────────
echo.
echo [2/6] Checking Python 3.11...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo      Python not found. Installing... (1-2 min)
    winget install -e --id Python.Python.3.11 --silent --accept-package-agreements --accept-source-agreements
    set "PATH=%PATH%;%LOCALAPPDATA%\Programs\Python\Python311;%LOCALAPPDATA%\Programs\Python\Python311\Scripts"
    refreshenv >nul 2>&1
    echo      Python installed
) else (
    echo      Python already installed
)

:: ── 3. Install Git ─────────────────────────────────────────────────────────
echo.
echo [3/6] Checking Git...
git --version >nul 2>&1
if %errorlevel% neq 0 (
    echo      Git not found. Installing... (1-2 min)
    winget install -e --id Git.Git --silent --accept-package-agreements --accept-source-agreements
    set "PATH=%PATH%;C:\Program Files\Git\cmd"
    echo      Git installed
) else (
    echo      Git already installed
)

:: ── 4. Clone project ───────────────────────────────────────────────────────
echo.
echo [4/6] Downloading project...
set "PROJECT_DIR=%USERPROFILE%\crawl-project"
if exist "%PROJECT_DIR%" (
    echo      Already exists. Pulling latest code...
    cd /d "%PROJECT_DIR%"
    git pull origin main
) else (
    git clone https://github.com/kangdotsukha-oss/crawl-project.git "%PROJECT_DIR%"
    cd /d "%PROJECT_DIR%"
    echo      Download complete
)

:: ── 5. Install Python packages ─────────────────────────────────────────────
echo.
echo [5/6] Installing Python packages... (2-3 min)
python -m pip install --upgrade pip --quiet
python -m pip install -r requirements.txt --quiet
echo      Packages installed

:: ── 6. .env setup ─────────────────────────────────────────────────────────
echo.
echo [6/6] Environment setup...
if not exist "%PROJECT_DIR%\.env" (
    echo.
    echo      .env file not found.
    echo      Please copy your .env file to: %PROJECT_DIR%\.env
    echo      Then press any key to continue...
    pause >nul
) else (
    echo      .env file found (skipping)
)

:: ── 7. Task Scheduler ──────────────────────────────────────────────────────
echo.
echo [7/7] Registering Task Scheduler (daily 07:00, 16:00)...

:: Delete existing tasks
schtasks /delete /tn "CrawlerAM" /f >nul 2>&1
schtasks /delete /tn "CrawlerPM" /f >nul 2>&1

:: 07:00 AM
schtasks /create /tn "CrawlerAM" /tr "cmd /c cd /d %PROJECT_DIR% && python crawl.py >> %PROJECT_DIR%\cron.log 2>&1" /sc daily /st 07:00 /ru "%USERNAME%" /f >nul
:: 04:00 PM
schtasks /create /tn "CrawlerPM" /tr "cmd /c cd /d %PROJECT_DIR% && python crawl.py >> %PROJECT_DIR%\cron.log 2>&1" /sc daily /st 16:00 /ru "%USERNAME%" /f >nul

echo      Task Scheduler registered

:: ── Done ───────────────────────────────────────────────────────────────────
echo.
echo ================================================
echo   Setup Complete!
echo ================================================
echo.
echo   Install path : %PROJECT_DIR%
echo   Log file     : %PROJECT_DIR%\cron.log
echo   Auto run     : daily 07:00, 16:00
echo.
echo   To test now:
echo   cd %PROJECT_DIR%
echo   python crawl.py --test 3
echo.

set /p DO_MIGRATE="Run GSheets migration now? (y/n): "
if /i "!DO_MIGRATE!"=="y" (
    echo.
    echo Running GSheets migration...
    cd /d "%PROJECT_DIR%"
    python migrate_gsheets.py
)

echo.
echo All done!
pause
