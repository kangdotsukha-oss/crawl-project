#!/bin/bash
# Oracle Cloud VM 자동 세팅 스크립트
# Ubuntu 22.04 LTS (ARM/AMD 모두 지원)
# 실행: bash setup_oracle.sh

set -e

echo "===== 1. 시스템 업데이트 ====="
sudo apt-get update -y
sudo apt-get upgrade -y

echo "===== 2. Python 3.11 설치 ====="
sudo apt-get install -y python3.11 python3.11-venv python3-pip python3.11-dev
sudo update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1
sudo update-alternatives --install /usr/bin/python python /usr/bin/python3.11 1

echo "===== 3. Chrome 설치 ====="
# ARM 여부 확인
ARCH=$(uname -m)
if [ "$ARCH" = "aarch64" ]; then
    # ARM: chromium 사용
    sudo apt-get install -y chromium-browser chromium-chromedriver
    CHROME_BIN=$(which chromium-browser)
    CHROMEDRIVER_BIN=$(which chromedriver)
else
    # AMD x86_64: Google Chrome 사용
    wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
    sudo apt-get install -y ./google-chrome-stable_current_amd64.deb
    rm google-chrome-stable_current_amd64.deb
    # ChromeDriver
    CHROME_VERSION=$(google-chrome --version | grep -oP '\d+\.\d+\.\d+')
    pip3 install webdriver-manager
fi

echo "===== 4. 기타 패키지 설치 ====="
sudo apt-get install -y git curl unzip fonts-nanum

echo "===== 5. 프로젝트 클론 ====="
cd ~
if [ ! -d "crawl-project" ]; then
    git clone https://github.com/kangdotsukha-oss/crawl-project.git
fi
cd crawl-project

echo "===== 6. Python 패키지 설치 ====="
pip3 install -r requirements.txt

echo "===== 7. .env 파일 생성 ====="
if [ ! -f ".env" ]; then
    cat > .env << 'ENVEOF'
ANTHROPIC_API_KEY=여기에_API_키_입력
GOOGLE_SHEET_ID=여기에_시트_ID_입력
GOOGLE_CREDENTIALS_JSON=여기에_서비스계정_JSON_한줄로_입력
ENVEOF
    echo ">>> .env 파일 생성됨. 값을 직접 입력하세요: nano ~/.crawl-project/.env"
fi

echo "===== 8. ARM Chrome 경로 설정 (ARM인 경우) ====="
if [ "$ARCH" = "aarch64" ]; then
    # selenium이 chromium을 찾을 수 있도록 심볼릭 링크
    sudo ln -sf $(which chromium-browser) /usr/bin/google-chrome 2>/dev/null || true
fi

echo "===== 9. crontab 등록 (오전 7시 / 오후 4시 KST) ====="
# KST 07:00 = UTC 22:00 (전날), KST 16:00 = UTC 07:00
CRON_JOB="0 22 * * * cd $HOME/crawl-project && python3 crawl.py >> $HOME/crawl-project/cron.log 2>&1
0 7 * * * cd $HOME/crawl-project && python3 crawl.py >> $HOME/crawl-project/cron.log 2>&1"

(crontab -l 2>/dev/null | grep -v crawl\.py; echo "$CRON_JOB") | crontab -

echo ""
echo "===== 설치 완료 ====="
echo "남은 작업:"
echo "  1. nano ~/crawl-project/.env          → API 키 / 시트 ID / 서비스계정 JSON 입력"
echo "  2. python3 ~/crawl-project/migrate_gsheets.py  → GSheets 사이트목록 마이그레이션"
echo "  3. python3 ~/crawl-project/crawl.py --test 3   → 동작 확인"
echo "  4. crontab -l                          → 스케줄 확인"
