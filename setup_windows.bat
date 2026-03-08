@echo off
setlocal enabledelayedexpansion

echo ================================================
echo   Crawler One-Stop Setup (Windows)
echo ================================================
echo.

:: 式式 Check admin privileges 式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式
net session >/dev/null 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Please run as Administrator.
    echo Right-click this file and select "Run as administrator"
    pause
    exit /b 1
)

set "PROJECT_DIR=%USERPROFILE%\crawl-project"
set "PYTHON_DIR=%LOCALAPPDATA%\Programs\Python\Python311"
set "PYTHON_SCRIPTS=%PYTHON_DIR%\Scripts"

:: 式式 1. Check winget 式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式
echo [1/8] Checking winget...
winget --version >/dev/null 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] winget not found. Please install "App Installer" from Microsoft Store.
    pause
    exit /b 1
)
echo      OK

:: 式式 2. Install Python 式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式
echo.
echo [2/8] Checking Python...
python --version >/dev/null 2>&1
if %errorlevel% neq 0 (
    echo      Installing Python 3.11...
    winget install -e --id Python.Python.3.11 --silent --accept-package-agreements --accept-source-agreements
    set "PATH=%PYTHON_DIR%;%PYTHON_SCRIPTS%;%PATH%"
    echo      Python installed
) else (
    echo      OK (already installed)
)

:: 式式 3. Install Git 式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式
echo.
echo [3/8] Checking Git...
git --version >/dev/null 2>&1
if %errorlevel% neq 0 (
    echo      Installing Git...
    winget install -e --id Git.Git --silent --accept-package-agreements --accept-source-agreements
    set "PATH=%PATH%;C:\Program Files\Git\cmd"
    echo      Git installed
) else (
    echo      OK (already installed)
)

:: 式式 4. Install Google Chrome 式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式
echo.
echo [4/8] Checking Google Chrome...
if exist "C:\Program Files\Google\Chrome\Application\chrome.exe" (
    echo      OK (already installed)
) else (
    echo      Installing Google Chrome...
    winget install -e --id Google.Chrome --silent --accept-package-agreements --accept-source-agreements
    if exist "C:\Program Files\Google\Chrome\Application\chrome.exe" (
        echo      Chrome installed
    ) else (
        echo      [WARNING] Chrome install may need a reboot to complete.
    )
)

:: 式式 5. Clone / update project 式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式
echo.
echo [5/8] Downloading project...
if exist "%PROJECT_DIR%\" (
    echo      Already exists. Pulling latest code...
    cd /d "%PROJECT_DIR%"
    git pull origin main
) else (
    git clone https://github.com/kangdotsukha-oss/crawl-project.git "%PROJECT_DIR%"
    cd /d "%PROJECT_DIR%"
    echo      Download complete
)

:: 式式 6. Install Python packages 式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式
echo.
echo [6/8] Installing Python packages...
cd /d "%PROJECT_DIR%"
python -m pip install --upgrade pip --quiet
python -m pip install -r requirements.txt --quiet
python -m pip install python-dotenv --quiet
echo      OK

:: 式式 7. .env setup 式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式
echo.
echo [7/8] Checking .env...
if not exist "%PROJECT_DIR%\.env" (
    echo.
    echo      ============================================
    echo      .env file not found\!
    echo      Please copy your .env file to:
    echo        %PROJECT_DIR%\.env
    echo      ============================================
    echo.
    echo      Press any key after copying .env file...
    pause >/dev/null
    if not exist "%PROJECT_DIR%\.env" (
        echo.
        echo      [ERROR] .env still missing. Setup cannot continue.
        echo      Place your .env file and run this script again.
        pause
        exit /b 1
    )
)
echo      .env OK

:: 式式 8. Task Scheduler 式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式
echo.
echo [8/8] Registering scheduled tasks...

:: Delete ALL old tasks
for %%T in (CrawlerAM CrawlerPM WakeAM WakePM SleepAM SleepPM) do (
    schtasks /delete /tn "%%T" /f >/dev/null 2>&1
)

:: Find full python path for Task Scheduler
for /f "delims=" %%P in ('where python 2^>nul') do (
    set "PYTHON_PATH=%%P"
    goto :found_python
)
:found_python

:: Create launcher script
(
    echo @echo off
    echo taskkill /F /IM chromedriver.exe /T ^>/dev/null 2^>^&1
    echo taskkill /F /IM chrome.exe /T ^>/dev/null 2^>^&1
    echo timeout /t 3 /nobreak ^>/dev/null
    echo cd /d "%PROJECT_DIR%"
    echo "\!PYTHON_PATH\!" crawl.py ^>^> "%PROJECT_DIR%\cron.log" 2^>^&1
    echo taskkill /F /IM chromedriver.exe /T ^>/dev/null 2^>^&1
    echo taskkill /F /IM chrome.exe /T ^>/dev/null 2^>^&1
) > "%PROJECT_DIR%\run_crawl.bat"

:: Register Crawler tasks (Interactive + no battery restriction + 1hr limit)
powershell -NoProfile -Command ^
    " = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument '/c \"%PROJECT_DIR%\run_crawl.bat\"' -WorkingDirectory '%PROJECT_DIR%';" ^
    " = New-ScheduledTaskTrigger -Daily -At '07:00';" ^
    " = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 1);" ^
    " = New-ScheduledTaskPrincipal -UserId '%USERNAME%' -LogonType Interactive -RunLevel Highest;" ^
    "Register-ScheduledTask -TaskName 'CrawlerAM' -Action  -Trigger  -Settings  -Principal  -Force | Out-Null;" ^
    "Write-Host '     CrawlerAM (07:00) registered OK'"

powershell -NoProfile -Command ^
    " = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument '/c \"%PROJECT_DIR%\run_crawl.bat\"' -WorkingDirectory '%PROJECT_DIR%';" ^
    " = New-ScheduledTaskTrigger -Daily -At '16:00';" ^
    " = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 1);" ^
    " = New-ScheduledTaskPrincipal -UserId '%USERNAME%' -LogonType Interactive -RunLevel Highest;" ^
    "Register-ScheduledTask -TaskName 'CrawlerPM' -Action  -Trigger  -Settings  -Principal  -Force | Out-Null;" ^
    "Write-Host '     CrawlerPM (16:00) registered OK'"

:: 式式 Done 式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式式
echo.
echo ================================================
echo   Setup Complete\!
echo ================================================
echo.
echo   Install path : %PROJECT_DIR%
echo   Python       : \!PYTHON_PATH\!
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
