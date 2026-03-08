@echo off
setlocal enabledelayedexpansion

echo ================================================
echo   Crawler Auto Setup Script (Windows)
echo ================================================
echo.

:: ── Check admin privileges ────────────────────────────────────────────────
net session >/dev/null 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Please run as Administrator.
    echo Right-click this file and select "Run as administrator"
    pause
    exit /b 1
)

set "PROJECT_DIR=%USERPROFILE%\crawl-project"

:: ── 1. Check winget ───────────────────────────────────────────────────────
echo [1/7] Checking winget...
winget --version >/dev/null 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] winget not found. Please install "App Installer" from Microsoft Store.
    pause
    exit /b 1
)
echo      OK

:: ── 2. Install Python ─────────────────────────────────────────────────────
echo.
echo [2/7] Checking Python...
python --version >/dev/null 2>&1
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
git --version >/dev/null 2>&1
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
    pause >/dev/null
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

:: Delete ALL old tasks
for %%T in (CrawlerAM CrawlerPM WakeAM WakePM SleepAM SleepPM) do (
    schtasks /delete /tn "%%T" /f >/dev/null 2>&1
)

:: Create launcher script (zombie Chrome cleanup + crawling)
(
    echo @echo off
    echo taskkill /F /IM chromedriver.exe /T ^>/dev/null 2^>^&1
    echo taskkill /F /IM chrome.exe /T ^>/dev/null 2^>^&1
    echo timeout /t 3 /nobreak ^>/dev/null
    echo cd /d "%PROJECT_DIR%"
    echo python crawl.py ^>^> "%PROJECT_DIR%\cron.log" 2^>^&1
    echo taskkill /F /IM chromedriver.exe /T ^>/dev/null 2^>^&1
    echo taskkill /F /IM chrome.exe /T ^>/dev/null 2^>^&1
) > "%PROJECT_DIR%\run_crawl.bat"

:: -- Crawler tasks (Interactive session for Chrome stability) --
powershell -NoProfile -Command ^
    "$a = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument '/c \"%PROJECT_DIR%\run_crawl.bat\"' -WorkingDirectory '%PROJECT_DIR%';" ^
    "$t = New-ScheduledTaskTrigger -Daily -At '07:00';" ^
    "$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 1);" ^
    "$p = New-ScheduledTaskPrincipal -UserId '%USERNAME%' -LogonType Interactive -RunLevel Highest;" ^
    "Register-ScheduledTask -TaskName 'CrawlerAM' -Action $a -Trigger $t -Settings $s -Principal $p -Force | Out-Null;" ^
    "Write-Host '     CrawlerAM registered OK'"

powershell -NoProfile -Command ^
    "$a = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument '/c \"%PROJECT_DIR%\run_crawl.bat\"' -WorkingDirectory '%PROJECT_DIR%';" ^
    "$t = New-ScheduledTaskTrigger -Daily -At '16:00';" ^
    "$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 1);" ^
    "$p = New-ScheduledTaskPrincipal -UserId '%USERNAME%' -LogonType Interactive -RunLevel Highest;" ^
    "Register-ScheduledTask -TaskName 'CrawlerPM' -Action $a -Trigger $t -Settings $s -Principal $p -Force | Out-Null;" ^
    "Write-Host '     CrawlerPM registered OK'"

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
echo     07:00  Crawling (AM)
echo     16:00  Crawling (PM)
echo.
echo   To test now:
echo     cd %PROJECT_DIR%
echo     python crawl.py --test 3
echo.
pause
