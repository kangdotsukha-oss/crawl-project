@echo off
setlocal enabledelayedexpansion

echo ================================================
echo   Crawler One-Stop Setup (Windows)
echo ================================================
echo.

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Please run as Administrator.
    pause
    exit /b 1
)

set "PROJECT_DIR=%USERPROFILE%\crawl-project"

echo [1/8] Checking winget...
winget --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] winget not found.
    pause
    exit /b 1
)
echo      OK

echo.
echo [2/8] Checking Python...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo      Installing Python 3.11...
    winget install -e --id Python.Python.3.11 --silent --accept-package-agreements --accept-source-agreements
    set "PATH=%LOCALAPPDATA%\Programs\Python\Python311;%LOCALAPPDATA%\Programs\Python\Python311\Scripts;%PATH%"
    echo      Installed
) else (
    echo      OK
)

echo.
echo [3/8] Checking Git...
git --version >nul 2>&1
if %errorlevel% neq 0 (
    echo      Installing Git...
    winget install -e --id Git.Git --silent --accept-package-agreements --accept-source-agreements
    set "PATH=%PATH%;C:\Program Files\Git\cmd"
    echo      Installed
) else (
    echo      OK
)

echo.
echo [4/8] Checking Google Chrome...
reg query "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe" >nul 2>&1
if %errorlevel% neq 0 (
    echo      Installing Chrome...
    winget install -e --id Google.Chrome --silent --accept-package-agreements --accept-source-agreements
    echo      Installed
) else (
    echo      OK
)

echo.
echo [5/8] Downloading project...
if exist "%PROJECT_DIR%\crawl.py" (
    echo      Already exists. Pulling latest...
    cd /d "%PROJECT_DIR%"
    git pull origin main
) else (
    if exist "%PROJECT_DIR%" rmdir /s /q "%PROJECT_DIR%"
    git clone https://github.com/kangdotsukha-oss/crawl-project.git "%PROJECT_DIR%"
    cd /d "%PROJECT_DIR%"
    echo      Done
)

echo.
echo [6/8] Installing Python packages...
cd /d "%PROJECT_DIR%"
python -m pip install --upgrade pip --quiet 2>nul
python -m pip install -r requirements.txt --quiet 2>nul
python -m pip install python-dotenv --quiet 2>nul
echo      OK

echo.
echo [7/8] Checking .env...
if not exist "%PROJECT_DIR%\.env" (
    echo.
    echo      .env file not found. Creating it now...
    echo.
    set /p "API_KEY=      Enter DATA_GO_KR API Key: "
    set /p "SHEET_ID=      Enter GOOGLE_SHEET_ID (empty if none): "
    set /p "GOOG_CRED=      Enter GOOGLE_CREDENTIALS_JSON path (empty if none): "
    (
        echo DATA_GO_KR_API_KEY=!API_KEY!
        echo GOOGLE_SHEET_ID=!SHEET_ID!
        echo GOOGLE_CREDENTIALS_JSON=!GOOG_CRED!
    ) > "%PROJECT_DIR%\.env"
    echo.
    echo      .env created at %PROJECT_DIR%\.env
)
echo      OK

echo.
echo [8/8] Registering scheduled tasks...

schtasks /delete /tn "CrawlerAM" /f >nul 2>&1
schtasks /delete /tn "CrawlerPM" /f >nul 2>&1
schtasks /delete /tn "WakeAM" /f >nul 2>&1
schtasks /delete /tn "WakePM" /f >nul 2>&1
schtasks /delete /tn "SleepAM" /f >nul 2>&1
schtasks /delete /tn "SleepPM" /f >nul 2>&1

set "PYTHON_PATH=python"
for /f "delims=" %%P in ('where python 2^>nul') do (
    set "PYTHON_PATH=%%P"
    goto :got_python
)
:got_python
echo      Python: !PYTHON_PATH!

:: Create run_crawl.bat via PowerShell to avoid escaping hell
powershell -NoProfile -Command "Set-Content -Path '%PROJECT_DIR%\run_crawl.bat' -Encoding ASCII -Value @('@echo off','taskkill /F /IM chromedriver.exe /T >nul 2>&1','taskkill /F /IM chrome.exe /T >nul 2>&1','timeout /t 3 /nobreak >nul','cd /d %PROJECT_DIR%','!PYTHON_PATH! crawl.py >> %PROJECT_DIR%\cron.log 2>&1','taskkill /F /IM chromedriver.exe /T >nul 2>&1','taskkill /F /IM chrome.exe /T >nul 2>&1')"
echo      run_crawl.bat created

:: Create run_monitor.bat for design service monitoring
powershell -NoProfile -Command "Set-Content -Path '%PROJECT_DIR%\run_monitor_task.bat' -Encoding ASCII -Value @('@echo off','cd /d %PROJECT_DIR%','!PYTHON_PATH! monitor_design.py --days 30 >> %PROJECT_DIR%\monitor.log 2>&1')"
echo      run_monitor_task.bat created

:: Register tasks via schtasks
schtasks /create /tn "CrawlerAM" /tr "cmd /c \"%PROJECT_DIR%\run_crawl.bat\"" /sc daily /st 07:00 /rl highest /f
if %errorlevel% equ 0 (echo      CrawlerAM 07:00 OK) else (echo      [ERROR] CrawlerAM failed)

schtasks /create /tn "CrawlerPM" /tr "cmd /c \"%PROJECT_DIR%\run_crawl.bat\"" /sc daily /st 16:00 /rl highest /f
if %errorlevel% equ 0 (echo      CrawlerPM 16:00 OK) else (echo      [ERROR] CrawlerPM failed)

:: Design service monitoring (08:00 daily)
schtasks /delete /tn "DesignMonitor" /f >nul 2>&1
schtasks /create /tn "DesignMonitor" /tr "cmd /c \"%PROJECT_DIR%\run_monitor_task.bat\"" /sc daily /st 08:00 /rl highest /f
if %errorlevel% equ 0 (echo      DesignMonitor 08:00 OK) else (echo      [ERROR] DesignMonitor failed)

echo.
echo ================================================
echo   Setup Complete!
echo ================================================
echo.
echo   Path: %PROJECT_DIR%
echo   Log:  %PROJECT_DIR%\cron.log
echo   Monitor Log: %PROJECT_DIR%\monitor.log
echo.
echo   Schedule:
echo     07:00  Crawling AM
echo     08:00  Design Monitor
echo     16:00  Crawling PM
echo.
echo   Test crawl:   cd %PROJECT_DIR% ^& python crawl.py --test 3
echo   Test monitor: cd %PROJECT_DIR% ^& python monitor_design.py --days 7 --no-upload
echo.
pause
