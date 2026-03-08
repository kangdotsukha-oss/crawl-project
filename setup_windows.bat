@echo off
setlocal enabledelayedexpansion

echo ================================================
echo   Crawler Auto Setup Script (Windows)
echo ================================================
echo.

:: ── Check admin privileges ────────────────────────────────────────────────
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Please run as Administrator.
    echo Right-click this file and select "Run as administrator"
    pause
    exit /b 1
)

set "PROJECT_DIR=%USERPROFILE%\crawl-project"

:: ── 1. Check winget ───────────────────────────────────────────────────────
echo [1/7] Checking winget...
winget --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] winget not found. Please install "App Installer" from Microsoft Store.
    pause
    exit /b 1
)
echo      OK

:: ── 2. Install Python ─────────────────────────────────────────────────────
echo.
echo [2/7] Checking Python...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo      Installing Python 3.11... (1-2 min)
    winget install -e --id Python.Python.3.11 --silent --accept-package-agreements --accept-source-agreements
    set "PATH=%PATH%;%LOCALAPPDATA%\Programs\Python\Python311;%LOCALAPPDATA%\Programs\Python\Python311\Scripts"
    echo      Python installed
) else (
    echo      OK (already installed)
)

:: ── 3. Install Git ────────────────────────────────────────────────────────
echo.
echo [3/7] Checking Git...
git --version >nul 2>&1
if %errorlevel% neq 0 (
    echo      Installing Git... (1-2 min)
    winget install -e --id Git.Git --silent --accept-package-agreements --accept-source-agreements
    set "PATH=%PATH%;C:\Program Files\Git\cmd"
    echo      Git installed
) else (
    echo      OK (already installed)
)

:: ── 4. Clone / update project ─────────────────────────────────────────────
echo.
echo [4/7] Downloading project...
if exist "%PROJECT_DIR%\" (
    echo      Already exists. Pulling latest code...
    cd /d "%PROJECT_DIR%"
    git pull origin main
) else (
    git clone https://github.com/kangdotsukha-oss/crawl-project.git "%PROJECT_DIR%"
    cd /d "%PROJECT_DIR%"
    echo      Download complete
)

:: ── 5. Install Python packages ────────────────────────────────────────────
echo.
echo [5/7] Installing Python packages... (2-3 min)
cd /d "%PROJECT_DIR%"
python -m pip install --upgrade pip --quiet
python -m pip install -r requirements.txt --quiet
echo      OK

:: ── 6. .env setup ────────────────────────────────────────────────────────
echo.
echo [6/7] Checking .env...
if not exist "%PROJECT_DIR%\.env" (
    echo.
    echo      [!] .env file not found!
    echo      Copy your .env file to: %PROJECT_DIR%
    echo      Then press any key to continue...
    pause >nul
    if not exist "%PROJECT_DIR%\.env" (
        echo      [WARNING] .env still missing. Crawler will not work without it.
    ) else (
        echo      .env OK
    )
) else (
    echo      OK
)

:: ── 7. Task Scheduler ────────────────────────────────────────────────────
echo.
echo [7/7] Registering scheduled tasks...

:: Enable wake timers in power settings
powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1 >nul 2>&1
powercfg /setdcvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1 >nul 2>&1
powercfg /setactive SCHEME_CURRENT >nul 2>&1

:: Delete existing tasks
for %%T in (CrawlerAM CrawlerPM WakeAM WakePM SleepAM SleepPM) do (
    schtasks /delete /tn "%%T" /f >nul 2>&1
)

:: -- Wake tasks (PC wakes from sleep) via PowerShell --
powershell -NoProfile -Command ^
    "$s = New-ScheduledTaskSettingsSet -WakeToRun;" ^
    "$a = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument '/c echo wake';" ^
    "$t = New-ScheduledTaskTrigger -Daily -At '06:50';" ^
    "Register-ScheduledTask -TaskName 'WakeAM' -Action $a -Trigger $t -Settings $s -Force | Out-Null;" ^
    "$t2 = New-ScheduledTaskTrigger -Daily -At '15:50';" ^
    "Register-ScheduledTask -TaskName 'WakePM' -Action $a -Trigger $t2 -Settings $s -Force | Out-Null;" ^
    "Write-Host '     Wake tasks registered OK'"

:: -- Crawler tasks --
schtasks /create /tn "CrawlerAM" ^
    /tr "cmd /c cd /d \"%PROJECT_DIR%\" && python crawl.py >> \"%PROJECT_DIR%\cron.log\" 2>&1" ^
    /sc daily /st 07:00 /f
if %errorlevel% equ 0 (echo      CrawlerAM registered OK) else (echo [ERROR] CrawlerAM failed)

schtasks /create /tn "CrawlerPM" ^
    /tr "cmd /c cd /d \"%PROJECT_DIR%\" && python crawl.py >> \"%PROJECT_DIR%\cron.log\" 2>&1" ^
    /sc daily /st 16:00 /f
if %errorlevel% equ 0 (echo      CrawlerPM registered OK) else (echo [ERROR] CrawlerPM failed)

:: -- Sleep tasks (PC goes to sleep after crawling) --
schtasks /create /tn "SleepAM" ^
    /tr "rundll32.exe powrprof.dll,SetSuspendState 0,1,0" ^
    /sc daily /st 07:30 /f
if %errorlevel% equ 0 (echo      SleepAM registered OK) else (echo [ERROR] SleepAM failed)

schtasks /create /tn "SleepPM" ^
    /tr "rundll32.exe powrprof.dll,SetSuspendState 0,1,0" ^
    /sc daily /st 16:30 /f
if %errorlevel% equ 0 (echo      SleepPM registered OK) else (echo [ERROR] SleepPM failed)

:: ── Done ─────────────────────────────────────────────────────────────────
echo.
echo ================================================
echo   Setup Complete!
echo ================================================
echo.
echo   Install path : %PROJECT_DIR%
echo   Log file     : %PROJECT_DIR%\cron.log
echo.
echo   Schedule:
echo     06:50  PC wakes up
echo     07:00  Crawling starts
echo     07:30  PC goes to sleep
echo     15:50  PC wakes up
echo     16:00  Crawling starts
echo     16:30  PC goes to sleep
echo.
echo   To test now:
echo     cd %PROJECT_DIR%
echo     python crawl.py --test 3
echo.
pause
