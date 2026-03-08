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
    echo      .env file not found!
    echo      Copy your .env to: %PROJECT_DIR%\.env
    echo      Then press any key...
    pause >nul
)
if not exist "%PROJECT_DIR%\.env" (
    echo      [ERROR] .env missing. Run again after placing .env
    pause
    exit /b 1
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

echo @echo off> "%PROJECT_DIR%\run_crawl.bat"
echo taskkill /F /IM chromedriver.exe /T ^>nul 2^>^&1>> "%PROJECT_DIR%\run_crawl.bat"
echo taskkill /F /IM chrome.exe /T ^>nul 2^>^&1>> "%PROJECT_DIR%\run_crawl.bat"
echo timeout /t 3 /nobreak ^>nul>> "%PROJECT_DIR%\run_crawl.bat"
echo cd /d "%PROJECT_DIR%">> "%PROJECT_DIR%\run_crawl.bat"
echo "!PYTHON_PATH!" crawl.py ^>^> "%PROJECT_DIR%\cron.log" 2^>^&1>> "%PROJECT_DIR%\run_crawl.bat"
echo taskkill /F /IM chromedriver.exe /T ^>nul 2^>^&1>> "%PROJECT_DIR%\run_crawl.bat"
echo taskkill /F /IM chrome.exe /T ^>nul 2^>^&1>> "%PROJECT_DIR%\run_crawl.bat"

powershell -NoProfile -Command "$dir = $env:USERPROFILE + '\crawl-project'; $a = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument ('/c ' + [char]34 + $dir + '\run_crawl.bat' + [char]34) -WorkingDirectory $dir; $t = New-ScheduledTaskTrigger -Daily -At '07:00'; $s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 1); $p = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest; Register-ScheduledTask -TaskName 'CrawlerAM' -Action $a -Trigger $t -Settings $s -Principal $p -Force | Out-Null; Write-Host '     CrawlerAM (07:00) OK'"

powershell -NoProfile -Command "$dir = $env:USERPROFILE + '\crawl-project'; $a = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument ('/c ' + [char]34 + $dir + '\run_crawl.bat' + [char]34) -WorkingDirectory $dir; $t = New-ScheduledTaskTrigger -Daily -At '16:00'; $s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 1); $p = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest; Register-ScheduledTask -TaskName 'CrawlerPM' -Action $a -Trigger $t -Settings $s -Principal $p -Force | Out-Null; Write-Host '     CrawlerPM (16:00) OK'"

echo.
echo ================================================
echo   Setup Complete!
echo ================================================
echo.
echo   Path: %PROJECT_DIR%
echo   Log:  %PROJECT_DIR%\cron.log
echo.
echo   Schedule:
echo     07:00  Crawling AM
echo     16:00  Crawling PM
echo.
echo   Test: cd %PROJECT_DIR% ^& python crawl.py --test 3
echo.
pause
