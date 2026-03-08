@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

echo ================================================
echo   크롤러 자동 세팅 스크립트 (Windows)
echo ================================================
echo.

:: 관리자 권한 확인
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [오류] 관리자 권한으로 실행해주세요.
    echo 이 파일을 우클릭 후 "관리자 권한으로 실행" 선택
    pause
    exit /b 1
)

:: ── 1. winget 확인 ────────────────────────────────────────────────────────
echo [1/6] 패키지 관리자 확인...
winget --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [오류] winget이 없습니다. Windows 10 1709 이상에서 지원됩니다.
    echo Microsoft Store에서 "앱 설치 관리자"를 설치해주세요.
    pause
    exit /b 1
)
echo      winget 확인 완료

:: ── 2. Python 설치 ────────────────────────────────────────────────────────
echo.
echo [2/6] Python 3.11 설치 확인...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo      Python이 없습니다. 설치 중... (1~2분 소요)
    winget install -e --id Python.Python.3.11 --silent --accept-package-agreements --accept-source-agreements
    :: PATH 갱신
    set "PATH=%PATH%;%LOCALAPPDATA%\Programs\Python\Python311;%LOCALAPPDATA%\Programs\Python\Python311\Scripts"
    refreshenv >nul 2>&1
    echo      Python 설치 완료
) else (
    echo      Python 이미 설치됨
)

:: ── 3. Git 설치 ───────────────────────────────────────────────────────────
echo.
echo [3/6] Git 설치 확인...
git --version >nul 2>&1
if %errorlevel% neq 0 (
    echo      Git이 없습니다. 설치 중... (1~2분 소요)
    winget install -e --id Git.Git --silent --accept-package-agreements --accept-source-agreements
    set "PATH=%PATH%;C:\Program Files\Git\cmd"
    echo      Git 설치 완료
) else (
    echo      Git 이미 설치됨
)

:: ── 4. 프로젝트 clone ─────────────────────────────────────────────────────
echo.
echo [4/6] 프로젝트 다운로드...
set "PROJECT_DIR=%USERPROFILE%\crawl-project"
if exist "%PROJECT_DIR%" (
    echo      이미 존재함. 최신 코드로 업데이트...
    cd /d "%PROJECT_DIR%"
    git pull origin main
) else (
    git clone https://github.com/kangdotsukha-oss/crawl-project.git "%PROJECT_DIR%"
    cd /d "%PROJECT_DIR%"
    echo      다운로드 완료
)

:: ── 5. Python 패키지 설치 ─────────────────────────────────────────────────
echo.
echo [5/6] Python 패키지 설치 중... (2~3분 소요)
python -m pip install --upgrade pip --quiet
python -m pip install -r requirements.txt --quiet
echo      패키지 설치 완료

:: ── 6. .env 파일 설정 ────────────────────────────────────────────────────
echo.
echo [6/6] 환경 설정...
if not exist "%PROJECT_DIR%\.env" (
    echo.
    echo ================================================
    echo   API 키 설정 (아래에 값을 입력해주세요)
    echo ================================================
    echo.
    set /p ANTHROPIC_KEY="ANTHROPIC_API_KEY 입력: "
    set /p SHEET_ID="GOOGLE_SHEET_ID 입력: "
    echo.
    echo GOOGLE_CREDENTIALS_JSON 은 별도로 입력합니다.
    echo (서비스 계정 JSON 파일 내용을 한 줄로 붙여넣기)
    set /p GCP_JSON="GOOGLE_CREDENTIALS_JSON 입력: "

    (
        echo ANTHROPIC_API_KEY=!ANTHROPIC_KEY!
        echo GOOGLE_SHEET_ID=!SHEET_ID!
        echo GOOGLE_CREDENTIALS_JSON=!GCP_JSON!
    ) > "%PROJECT_DIR%\.env"
    echo      .env 파일 생성 완료
) else (
    echo      .env 파일 이미 존재함 (건너뜀)
)

:: ── 7. 작업 스케줄러 등록 ────────────────────────────────────────────────
echo.
echo [7/7] 작업 스케줄러 등록 (매일 07:00, 16:00 자동 실행)...

:: 기존 작업 삭제 (있으면)
schtasks /delete /tn "CrawlerAM" /f >nul 2>&1
schtasks /delete /tn "CrawlerPM" /f >nul 2>&1

:: 오전 7시
schtasks /create /tn "CrawlerAM" /tr "cmd /c cd /d %PROJECT_DIR% && python crawl.py >> %PROJECT_DIR%\cron.log 2>&1" /sc daily /st 07:00 /ru "%USERNAME%" /f >nul
:: 오후 4시
schtasks /create /tn "CrawlerPM" /tr "cmd /c cd /d %PROJECT_DIR% && python crawl.py >> %PROJECT_DIR%\cron.log 2>&1" /sc daily /st 16:00 /ru "%USERNAME%" /f >nul

echo      작업 스케줄러 등록 완료

:: ── 완료 ─────────────────────────────────────────────────────────────────
echo.
echo ================================================
echo   설치 완료!
echo ================================================
echo.
echo   설치 경로: %PROJECT_DIR%
echo   로그 파일: %PROJECT_DIR%\cron.log
echo   자동 실행: 매일 07:00, 16:00
echo.
echo   지금 바로 테스트하려면:
echo   cd %PROJECT_DIR%
echo   python crawl.py --test 3
echo.

:: GSheets 마이그레이션 여부 확인
set /p DO_MIGRATE="GSheets 마이그레이션 지금 실행할까요? (y/n): "
if /i "!DO_MIGRATE!"=="y" (
    echo.
    echo GSheets 사이트목록 마이그레이션 중...
    cd /d "%PROJECT_DIR%"
    python migrate_gsheets.py
)

echo.
echo 모든 세팅이 완료되었습니다.
pause
