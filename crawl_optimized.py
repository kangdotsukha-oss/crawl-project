"""
공공기관 고시/공고 자동 크롤러 (v4) - 자가치유(Self-Healing) 버전
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
구글시트 탭 구성:
  - 사이트목록         : 크롤링 대상 229개 사이트
  - ✅키워드공고        : 특허/제안/공법 등 키워드 매칭 공고
  - 📋전체공고(키워드제외): 키워드 미해당 전체 공고
  - 크롤링로그         : 사이트별 크롤링 상태 및 오류 현황

자가치유 기능:
  - 셀렉터 오류 → Claude 분석 → 자동 수정 후 재크롤링
  - URL 404/연결실패 → Claude 웹서치로 새 URL 탐색
  - 웹방화벽 차단 → UA 로테이션으로 우회 시도
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import argparse
import requests
from bs4 import BeautifulSoup
import pandas as pd
import math
import re
import logging
import time
import gspread
import os
import json
import smtplib
from email.mime.text import MIMEText
from requests.packages.urllib3.exceptions import InsecureRequestWarning
from tqdm import tqdm
from datetime import datetime, timedelta, timezone
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.common.exceptions import TimeoutException, NoSuchElementException, ElementClickInterceptedException
from concurrent.futures import ThreadPoolExecutor, as_completed
from google.oauth2.service_account import Credentials
import threading

# .env 파일 지원 (Oracle/NAS 로컬 실행 시)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# KST = UTC+9
KST = timezone(timedelta(hours=9))

def now_kst() -> datetime:
    """현재 KST 시각 반환"""
    return datetime.now(KST)

SELF_HEALING_ENABLED = now_kst().weekday() == 0  # 월요일만 자가치유

# Claude API 동시 호출 방지 Lock (rate limit 대응)
_claude_lock = threading.Lock()

# URL 탐색 결과 캐시: 사이트당 1회만 Claude 웹서치 (다중 스레드 중복 방지)
_url_search_cache: dict = {}
_url_search_cache_lock = threading.Lock()

# 연속 5회 실패 사이트 자동 스킵 목록
_auto_skip_sites: set = set()

# ─────────────────────────────────────────────
# 로깅 설정 (KST 기준)
# ─────────────────────────────────────────────
class _KSTFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=KST)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime('%Y-%m-%d %H:%M:%S') + f',{int(record.msecs):03d}'

_fmt = _KSTFormatter("%(asctime)s [%(levelname)s] %(message)s")
_fh = logging.FileHandler("crawl.log", encoding="utf-8")
_fh.setFormatter(_fmt)
_sh = logging.StreamHandler()
_sh.setFormatter(_fmt)
logging.basicConfig(level=logging.INFO, handlers=[_fh, _sh])
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# 상수 설정
# ─────────────────────────────────────────────
MAX_PAGES = 5
FILTER_KEYWORDS = ['특허', '제안', '심의', '공법', '실시설계', '보수보강']
DAYS_RANGE = 1

# 지역 분류 매핑 (출처명 → 지역)
REGION_MAP = [
    ('서울', '서울'), ('부산', '부산'), ('대구', '대구'), ('인천', '인천'),
    ('광주', '광주'), ('대전', '대전'), ('울산', '울산'), ('세종', '세종'),
    ('경기', '경기도'), ('강원', '강원도'),
    ('충북', '충청도'), ('충남', '충청도'), ('충청', '충청도'),
    ('전북', '전라도'), ('전남', '전라도'), ('전라', '전라도'),
    ('경북', '경상도'), ('경남', '경상도'), ('경상', '경상도'),
    ('제주', '제주도'),
]

def extract_region(site_name: str) -> str:
    for key, region in REGION_MAP:
        if key in site_name:
            return region
    return '기타'

# 기본 헤더
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7"
}

# 웹방화벽 우회용 User-Agent 로테이션 목록
UA_ROTATION = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

# 웹방화벽 차단 감지 키워드
FIREWALL_KEYWORDS = [
    '웹방화벽', 'Web Firewall', 'WAPPLES', 'security policy', '보안정책',
    'Access Denied', '접근이 차단', '차단되었습니다', 'Blocked', 'Forbidden',
    '보안 위반', 'security violation', 'CloudFlare', 'Incapsula'
]

GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# 이메일 알림 설정 (선택사항 - GitHub Secrets에 등록)
EMAIL_SENDER = os.environ.get("EMAIL_SENDER", "")      # 발신 Gmail 주소
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "")  # Gmail 앱 비밀번호
EMAIL_RECEIVER = os.environ.get("EMAIL_RECEIVER", "")  # 수신 주소

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)


# ─────────────────────────────────────────────
# 유틸: 빈값 판별 (구글시트 "" / 엑셀 NaN 모두 처리)
# ─────────────────────────────────────────────
def is_empty(v) -> bool:
    if v is None or v == "":
        return True
    if isinstance(v, float) and math.isnan(v):
        return True
    return False


# ─────────────────────────────────────────────
# Google Sheets 연결
# ─────────────────────────────────────────────
def get_gspread_client():
    if not GOOGLE_CREDENTIALS_JSON:
        logger.warning("[Google Sheets] 환경변수 미설정")
        return None
    try:
        scopes = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive"
        ]
        creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        return gspread.authorize(creds)
    except Exception as e:
        logger.error(f"[Google Sheets 연결 오류] {e}")
        return None


# ─────────────────────────────────────────────
# 사이트 목록 로드
# ─────────────────────────────────────────────
def load_consecutive_failures(gc, threshold: int = 5) -> set:
    """크롤링로그에서 최근 threshold회 연속 실패한 사이트명 반환"""
    if gc is None:
        return set()
    try:
        sh = gc.open_by_key(GOOGLE_SHEET_ID)
        ws = sh.worksheet("크롤링로그")
        records = ws.get_all_records()
        if not records:
            return set()
        df_log = pd.DataFrame(records)
        skip_sites = set()
        for site_name, group in df_log.groupby('SITE_NAME'):
            recent = group.sort_values('수집일시', ascending=False).head(threshold)
            if len(recent) == threshold and all(
                str(s).startswith('실패') for s in recent['status']
            ):
                skip_sites.add(site_name)
                logger.info(f"[자동스킵 등록] {site_name}: {threshold}회 연속 실패")
        return skip_sites
    except Exception as e:
        logger.warning(f"[연속실패 확인 오류] {e}")
        return set()


def load_sites(gc) -> pd.DataFrame:
    if gc:
        try:
            sh = gc.open_by_key(GOOGLE_SHEET_ID)
            ws = sh.worksheet("사이트목록")
            df = pd.DataFrame(ws.get_all_records())
            logger.info(f"[사이트목록] 구글시트에서 {len(df)}개 로드")
            return df
        except Exception as e:
            logger.error(f"[사이트목록 로드 실패] {e} → 로컬 엑셀 사용")
    return pd.read_excel('./crawl_test.xlsx')


# ─────────────────────────────────────────────
# 구글시트 사이트목록 자동 업데이트
# ─────────────────────────────────────────────
def update_site_in_sheet(gc, site_no: str, updates: dict, original_url: str = None):
    """
    사이트목록 탭에서 SITE_NO + URL이 일치하는 행을 찾아 업데이트
    중복 SITE_NO(A108, A118, A155, A166 등)는 URL로 구분
    """
    if gc is None:
        return False
    try:
        sh = gc.open_by_key(GOOGLE_SHEET_ID)
        ws = sh.worksheet("사이트목록")
        records = ws.get_all_records()
        headers = ws.row_values(1)

        for i, record in enumerate(records, start=2):
            if str(record.get('SITE_NO', '')) != str(site_no):
                continue
            # URL도 일치해야 업데이트 (중복 SITE_NO 구분용)
            if original_url and str(record.get('URL', '')) != str(original_url):
                continue
            for col_name, new_val in updates.items():
                if col_name in headers:
                    col_idx = headers.index(col_name) + 1
                    ws.update_cell(i, col_idx, new_val)
            logger.info(f"[사이트목록 자동업데이트] {site_no}: {list(updates.keys())}")
            return True

        logger.warning(f"[사이트목록 업데이트 실패] SITE_NO={site_no} 찾을 수 없음")
        return False
    except Exception as e:
        logger.error(f"[사이트목록 업데이트 오류] {e}")
        return False


# ─────────────────────────────────────────────
# Selenium 드라이버
# ─────────────────────────────────────────────
def get_driver(timeout: int = 15, ua: str = None):
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    if ua:
        options.add_argument(f"--user-agent={ua}")
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(timeout)
    return driver


# ─────────────────────────────────────────────
# 웹방화벽 차단 감지
# ─────────────────────────────────────────────
def is_firewall_blocked(html: str, status_code: int = 200) -> bool:
    if status_code in (403, 406, 429):
        return True
    if not html:
        return False
    for kw in FIREWALL_KEYWORDS:
        if kw in html:
            return True
    return False


# ─────────────────────────────────────────────
# 크롤링 함수들
# ─────────────────────────────────────────────
def static_crawl(row, headers_override=None):
    h = headers_override or HEADERS
    try:
        res = requests.get(row['URL'], headers=h, timeout=(10, 30), verify=False)
        res.raise_for_status()
        res.encoding = 'utf-8'
        if is_firewall_blocked(res.text, res.status_code):
            logger.warning(f"[방화벽 차단 감지] {row['SITE_NAME']}")
            return None, 'firewall'
        return BeautifulSoup(res.text, 'html.parser'), 'ok'
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response else 0
        logger.error(f"[정적 크롤링 HTTP오류] {row['SITE_NAME']}: {code}")
        return None, f'http_{code}'
    except requests.exceptions.SSLError:
        # SSL 오류 시에만 유연한 세션으로 재시도
        logger.warning(f"[SSL 오류 → 폴백] {row['SITE_NAME']}")
        try:
            import ssl
            from requests.adapters import HTTPAdapter
            ctx = ssl.create_default_context()
            ctx.set_ciphers("DEFAULT@SECLEVEL=1")
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            if hasattr(ssl, 'OP_LEGACY_SERVER_CONNECT'):
                ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT

            class _SSLAdapter(HTTPAdapter):
                def init_poolmanager(self, *args, **kwargs):
                    kwargs['ssl_context'] = ctx
                    return super().init_poolmanager(*args, **kwargs)

            s = requests.Session()
            s.mount("https://", _SSLAdapter())
            res = s.get(row['URL'], headers=h, timeout=(10, 30), verify=False)
            res.raise_for_status()
            res.encoding = 'utf-8'
            if is_firewall_blocked(res.text, res.status_code):
                return None, 'firewall'
            logger.info(f"[SSL 폴백 성공] {row['SITE_NAME']}")
            return BeautifulSoup(res.text, 'html.parser'), 'ok'
        except Exception as e2:
            logger.error(f"[정적 크롤링 오류] {row['SITE_NAME']}: {e2}")
            return None, 'error'
    except Exception as e:
        logger.error(f"[정적 크롤링 오류] {row['SITE_NAME']}: {e}")
        return None, 'error'


def dynamic_crawl(row, wait=7, ua=None):
    driver = get_driver(ua=ua)
    try:
        try:
            driver.get(row['URL'])
        except TimeoutException:
            logger.warning(f"[로딩 타임아웃] {row['SITE_NAME']}")
        time.sleep(wait)
        html = driver.page_source
        if is_firewall_blocked(html):
            return None, 'firewall'
        return BeautifulSoup(html, 'html.parser'), 'ok'
    finally:
        driver.quit()


def dynamic_crawl_1(row):
    driver = get_driver()
    try:
        driver.get(row['URL'])
        time.sleep(7)
        driver.find_element(By.ID, 'ofr_pageSize').click()
        driver.find_element(By.XPATH, '//*[@id="ofr_pageSize"]/option[1]').click()
        time.sleep(3)
        return BeautifulSoup(driver.page_source, 'html.parser'), 'ok'
    finally:
        driver.quit()


def dynamic_crawl_2(row):
    driver = get_driver()
    try:
        driver.get(row['URL'])
        time.sleep(7)
        driver.find_element(By.CSS_SELECTOR, row['click_button']).click()
        time.sleep(3)
        return BeautifulSoup(driver.page_source, 'html.parser'), 'ok'
    finally:
        driver.quit()


def post_crawl(row):
    data = {
        'epcCheck': '', 'pageIndex': '', 'jndinm': 'OfrNotAncmtEJB',
        'context': 'NTIS', 'method': 'selectListOfrNotAncmt',
        'methodnm': 'selectListOfrNotAncmtHomepage', 'not_ancmt_mgt_no': '',
        'homepage_pbs_yn': 'Y', 'subCheck': 'N', 'ofr_pageSize': '10',
        'not_ancmt_se_code': '01,04,06', 'title': '고시공고',
        'cha_dep_code_nm': '', 'initValue': '', 'countYn': 'Y',
        'list_gubun': 'A', 'not_ancmt_sj': '', 'not_ancmt_cn': '',
        'dept_nm': '', 'cgg_code': '', 'yyyy': '', 'yyyymmdd': '',
        'recent_mm': '', 'last_mm': '', 'nodate_recent_mm': '',
        'nodate_last_mm': '', 'not_ancmt_reg_no': '', 'Key': 'B_Subject', 'temp': ''
    }
    res = requests.post(row['URL'], data=data).content
    return BeautifulSoup(res.decode('utf-8-sig'), 'html.parser'), 'ok'


# ─────────────────────────────────────────────
# 통합 클릭 크롤링
# ─────────────────────────────────────────────
CLICK_CRAWL_CONFIG = {
    'cd':  {'type': 'css',   'selector': "body > form > div.default_board > div.paging > table > tbody > tr > td:nth-child(4) > span:nth-child({(page_number-1)*2+1}) > a", 'wait': 5},
    'cd1': {'type': 'css',   'selector': "#form1 > div.pgeAbs.mt30 > p > span:nth-child({page_number}) > a", 'wait': 5},
    'cd2': {'type': 'xpath', 'selector': "/html/body/div[2]/div[2]/div/section[2]/div[1]/form/div[2]/a[{page_number+2}]", 'wait': 5},
    'cd3': {'type': 'css',   'selector': "#txt > div.text-center > div > ul > li:nth-child({page_number+2}) > a", 'wait': 5},
    'cd4': {'type': 'css',   'selector': "#dataForm > div.pagination.mt-md-4 > a:nth-child({page_number})", 'wait': 5},
    'cd5': {'type': 'css',   'selector': "#cont-body > div.paging > div > div > a:nth-child({page_number+2})", 'wait': 5},
    'cd6': {'type': 'css',   'selector': "#contentDiv > form > table.MAT10 > tbody > tr > td > table > tbody > tr > td > table > tbody > tr > td:nth-child(4) > span:nth-child({(page_number-1)*2+1}) > a", 'wait': 5},
    'cd7': {'type': 'css',   'selector': "#list > div.bod_page > a:nth-child({page_number+2})", 'wait': 5},
    'cd8': {'type': 'css',   'selector': "#board > div:nth-child(4) > table > tbody > tr > td > table > tbody > tr > td:nth-child(4) > span:nth-child({(page_number-1)*2+1}) > a", 'wait': 5},
    'cd9': {'type': 'css',   'selector': "body > form > div.sb_w > div:nth-child(3) > table > tbody > tr > td > table > tbody > tr > td:nth-child(4) > span:nth-child({(page_number-1)*2+1}) > a", 'wait': 9},
    'cd10': {'type': 'xpath','selector': "/html/body/form/table[3]/tbody/tr/td/table[2]/tbody/tr/td/table/tbody/tr/td/table/tbody/tr/td[4]/span[{(page_number-1)*2+1}]/a", 'wait': 7},
    'cd11': {'type': 'xpath','selector': "//*[@id='list']/div[2]/div/a[{page_number+2}]", 'wait': 7},
    'cd12': {'type': 'css',  'selector': "#sidoGosiAPIVO > div.pagination > div.normal_pagination > a:nth-child({page_number+2})", 'wait': 7},
    'cd13': {'type': 'css',  'selector': "#txt > div > div.text-center > ul > li:nth-child({page_number+2}) > a", 'wait': 7},
    'cd14': {'type': 'css',  'selector': "body > form > div > div > div.p-pagination > div > span.p-page__link-group > a:nth-child({page_number})", 'wait': 7},
    'cd15': {'type': 'css',  'selector': "body > form > div > div.paging > table > tbody > tr > td:nth-child(4) > span:nth-child({(page_number-1)*2+1}) > a", 'wait': 7},
    'cd16': {'type': 'css',  'selector': "body > form > table > tbody > tr:nth-child(2) > td:nth-child(2) > table > tbody > tr:nth-child(7) > td > table > tbody > tr > td:nth-child({page_number+4}) > a", 'wait': 7},
    'cd17': {'type': 'css',  'selector': "body > form > div.board > div > div > table > tbody > tr > td:nth-child(2) > table > tbody > tr > td:nth-child(4) > span:nth-child({(page_number-1)*2+1})", 'wait': 7},
    'cd18': {'type': 'css',  'selector': "#contents > div > div.p-wrap.bbs.bbs_list > div.p-pagination > div.p-page_link-group > a:nth-child({page_number})", 'wait': 7},
    'cd19': {'type': 'css',  'selector': "#contents > form > table:nth-child(23) > tbody > tr:nth-child(1) > td > table > tbody > tr > td > table > tbody > tr > td:nth-child(4) > span:nth-child({(page_number-1)*2+1}) > a", 'wait': 7},
    'cd20': {'type': 'css',  'selector': "body > form > div.pagination > a:nth-child({page_number})", 'wait': 7},
    'cd21': {'type': 'css',  'selector': "body > div.pagination > table > tbody > tr > td:nth-child(4) > span:nth-child({(page_number-1)*2+1}) > a", 'wait': 7},
    'cd22': {'type': 'css',  'selector': "body > div.pagination > a:nth-child({page_number})", 'wait': 7},
    'cd23': {'type': 'xpath','selector': "/html/body/div[4]/section/div/div/div[2]/div/div/div[3]/div/ul/ul/li[{page_number+2}]", 'wait': 7},
    'cd24': {'type': 'css',  'selector': "#content_area > div.container > div > div.content > div.board_list > div.paging > ul > li:nth-child({page_number}) > a", 'wait': 7},
    'cd25': {'type': 'css',  'selector': "#eminwonWrap > div.pagination > ul > li:nth-child({page_number}) > a", 'wait': 7},
    'cd26': {'type': 'css',  'selector': "#listForm > div.box_page > a:nth-child({page_number+2})", 'wait': 7},
    'cd27': {'type': 'xpath','selector': "/html/body/form/table[2]/tbody/tr/td/table[2]/tbody/tr/td/table/tbody/tr/td/table/tbody/tr/td[4]/span[{(page_number-1)*2+1}]/a", 'wait': 7},
    'cd28': {'type': 'css',  'selector': "#A-Contents > div.pager > table > tbody > tr > td > table > tbody > tr > td:nth-child(4) > span:nth-child({(page_number-1)*2+1}) > a", 'wait': 7},
    'cd29': {'type': 'css',  'selector': "#contentsArea > div.pager > a:nth-child({page_number+3})", 'wait': 7},
    'cd30': {'type': 'xpath','selector': "/html/body/form/div/table/tbody/tr/td/table/tbody/tr/td/table/tbody/tr/td[4]/span[{(page_number-1)*2+1}]", 'wait': 7},
    'cd31': {'type': 'css',  'selector': "body > form > section > div.pager > a:nth-child({page_number+2})", 'wait': 7},
    'cd32': {'type': 'xpath','selector': "/html/body/div/main/div/div/div[2]/div[2]/div[3]/a[{page_number+2}]", 'wait': 7},
    'cd33': {'type': 'css',  'selector': "body > form > table:nth-child(12) > tbody > tr > td > table:nth-child(3) > tbody > tr > td > table > tbody > tr > td > table > tbody > tr > td:nth-child(4) > span[{(page_number-1)*2+1}]/a", 'wait': 7},
    'cd34': {'type': 'css',  'selector': "#paging-tag > ul > li:nth-child({page_number+2})", 'wait': 7},
}


def click_dynamic_crawl(row, page_number):
    ct = row['crawl_type']
    config = CLICK_CRAWL_CONFIG.get(ct)
    if not config:
        raise ValueError(f"알 수 없는 crawl_type: {ct}")
    selector = eval(f'f"{config["selector"]}"')
    wait_time = config.get('wait', 5)
    driver = get_driver()
    try:
        driver.get(row['URL'])
        time.sleep(wait_time)
        if page_number > 1:
            by = By.CSS_SELECTOR if config['type'] == 'css' else By.XPATH
            try:
                btn = driver.find_element(by, selector)
                # 1) scrollIntoView 후 일반 클릭
                driver.execute_script("arguments[0].scrollIntoView(true);", btn)
                time.sleep(0.5)
                try:
                    btn.click()
                except (ElementClickInterceptedException, Exception):
                    # 2) JS 강제 클릭 (element click intercepted 대응)
                    driver.execute_script("arguments[0].click();", btn)
                time.sleep(wait_time)
            except NoSuchElementException:
                logger.warning(f"[버튼 없음] {row['SITE_NAME']} 페이지 {page_number}")
                driver.quit()
                return None, 'no_button'  # 버튼 없음 → 페이지 순회 중단 신호
        html = driver.page_source
        if is_firewall_blocked(html):
            return None, 'firewall'
        return BeautifulSoup(html, 'html.parser'), 'ok'
    finally:
        driver.quit()


# ─────────────────────────────────────────────
# URL 페이지 업데이트
# ─────────────────────────────────────────────
URL_PATTERNS = {
    "pageIndex=": lambda url, p: re.sub(r"pageIndex=\d+", f"pageIndex={p}", url),
    "page=":      lambda url, p: re.sub(r"page=\d+", f"page={p}", url),
    "Page=":      lambda url, p: re.sub(r"Page=\d+", f"Page={p}", url),
    "&cpn=":      lambda url, p: re.sub(r"&cpn=\d+", f"&cpn={p}", url),
    "pageNo=":    lambda url, p: re.sub(r"pageNo=\d+", f"pageNo={p}", url),
    "offset=":    lambda url, p: re.sub(r"offset=\d+", f"offset={(p-1)*15}", url),
    "?p=":        lambda url, p: re.sub(r"\?p=\d+", f"?p={p}", url),
    "Page2=":     lambda url, p: re.sub(r"Page2=\d+", f"Page2={p}", url),
    "Start=":     lambda url, p: re.sub(r"Start=\d+", f"Start={(p-1)*10}", url),
    "pageid=":    lambda url, p: re.sub(r"pageid=\d+", f"pageid={p}", url),
}


def update_url_for_next_page(url, page_number, div):
    if page_number == 1:
        return url
    if (div or '') != 'V2':
        return None
    for pattern, updater in URL_PATTERNS.items():
        if pattern in url:
            return updater(url, page_number)
    return url


def update_crawl_type(url, crawl_type, page_number, ct2):
    if page_number == 1:
        return crawl_type
    if not is_empty(ct2):
        return ct2
    return crawl_type


# ─────────────────────────────────────────────
# 날짜 처리
# ─────────────────────────────────────────────
def fix_date_format(date_str):
    if not date_str or not isinstance(date_str, str):
        return ""
    date_str = date_str.strip()

    # 8자리 숫자 → YYYY-MM-DD
    if len(date_str) == 8 and date_str.isdigit():
        return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"

    # 18자리 특수 포맷
    if len(date_str) == 18:
        return f"{date_str[:4]}-{date_str[5:7]}-{date_str[8:10]}"

    # ~ 범위에서 앞부분만 사용
    date_str = date_str.split('~')[0].strip()

    # 요일 제거: (Mon), (Sat) 등
    date_str = re.sub(r'\([A-Za-z]{3}\)', '', date_str).strip()

    # 끝 특수문자 제거: -, ., 공백 등
    date_str = date_str.rstrip('-. ')

    # YYYY-MM-DD 패턴만 추출 (garbage 방어)
    m = re.search(r'(20\d{2}[-./]\d{1,2}[-./]\d{1,2})', date_str)
    if m:
        date_str = m.group(1).replace('.', '-').replace('/', '-')
        return date_str

    # YY-MM-DD → 20YY-MM-DD
    parts = date_str.split('-')
    if parts and len(parts[0]) == 2:
        return '20' + date_str

    # 숫자만 있거나 날짜처럼 안 보이면 빈값 반환
    if not re.search(r'20\d{2}', date_str):
        return ""

    return date_str


def extract_date_from_text(text):
    if not text or not isinstance(text, str):
        return ""
    if '공고부서 :' in text:
        return text.split('공고부서 :')[-2].split('등록일 :')[-1].strip()
    elif '게재일 :' in text:
        return text.split('게재일 :')[1].strip()
    # 날짜 패턴 직접 추출 (텍스트가 길거나 잡동사니 섞인 경우 대비)
    m = re.search(r'(20\d{2}[-./]\d{1,2}[-./]\d{1,2})', text)
    if m:
        return m.group(1).replace('.', '-').replace('/', '-')
    return text.replace('.', '-').replace('/', '-').replace('등록일 :', '').strip()


# ─────────────────────────────────────────────
# HTML 수집 (분석용)
# ─────────────────────────────────────────────
def fetch_html_for_analysis(row, url_override=None) -> str | None:
    """분석용 HTML 수집 - Selenium 우선(동적 대응), 실패 시 정적"""
    site_name = row['SITE_NAME']
    url = url_override or row['URL']

    # 1) Selenium 6초 대기 (동적 페이지 완전 로드)
    for ua in [None, UA_ROTATION[1]]:  # 기본 UA, Mac UA 순으로 시도
        try:
            driver = get_driver(timeout=20, ua=ua)
            try:
                driver.get(url)
            except TimeoutException:
                pass
            time.sleep(6)
            html = driver.page_source
            driver.quit()

            soup_check = BeautifulSoup(html, 'html.parser')
            body = soup_check.find('body')
            body_text = body.get_text(strip=True) if body else ''
            if len(body_text) > 200 and not is_firewall_blocked(html):
                logger.info(f"[HTML수집-동적] {site_name}: {len(html)}bytes")
                return html
        except Exception as e:
            logger.warning(f"[HTML수집-동적 실패] {site_name}: {e}")

    # 2) 정적 요청
    for ua in UA_ROTATION[:3]:
        try:
            h = {**HEADERS, "User-Agent": ua}
            res = requests.get(url, headers=h, timeout=(10, 20), verify=False)
            res.encoding = 'utf-8'
            if len(res.text) > 1000 and not is_firewall_blocked(res.text, res.status_code):
                logger.info(f"[HTML수집-정적] {site_name}: {len(res.text)}bytes")
                return res.text
        except Exception:
            pass

    logger.error(f"[HTML수집 전체 실패] {site_name}")
    return None


# ─────────────────────────────────────────────
# Claude API 호출 공통 함수
# ─────────────────────────────────────────────
def call_claude_api(prompt: str, tools: list = None, max_tokens: int = 1000) -> dict | None:
    if not ANTHROPIC_API_KEY:
        logger.warning("[Claude API] API 키 미설정")
        return None

    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}]
    }
    if tools:
        payload["tools"] = tools

    with _claude_lock:  # 한 번에 하나씩만 Claude 호출
        try:
            response = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json=payload,
                timeout=60
            )
            if response.status_code != 200:
                logger.error(f"[Claude API] HTTP {response.status_code}: {response.text[:200]}")
                return None
            time.sleep(3)  # 연속 호출 rate limit 방지
            return response.json()
        except Exception as e:
            logger.error(f"[Claude API 오류] {type(e).__name__}: {e}")
            return None


def parse_claude_json(result: dict, site_name: str) -> dict | None:
    """Claude 응답에서 JSON 파싱"""
    if not result:
        return None
    content_list = result.get('content', [])
    if not content_list:
        return None

    # tool_use 블록 처리 (웹서치 결과)
    for block in content_list:
        if block.get('type') == 'text':
            text = block.get('text', '').strip()
            text = re.sub(r'```json\s*', '', text)
            text = re.sub(r'```\s*', '', text)
            text = text.strip()
            if text.startswith('{'):
                try:
                    return json.loads(text)
                except json.JSONDecodeError as e:
                    logger.error(f"[Claude JSON파싱오류] {site_name}: {e}")
    return None


# ─────────────────────────────────────────────
# 셀렉터 자동 분석 (Claude)
# ─────────────────────────────────────────────
def analyze_with_claude(site_name: str, url: str, html: str) -> dict | None:
    """HTML에서 CSS 셀렉터 자동 분석"""
    # script/style 제거 후 body만 추출
    try:
        soup_trim = BeautifulSoup(html, 'html.parser')
        for tag in soup_trim(['script', 'style', 'link', 'meta']):
            tag.decompose()
        body = soup_trim.find('body')
        html_trimmed = str(body)[:20000] if body else html[:20000]
    except Exception:
        html_trimmed = html[:20000]

    prompt = f"""아래는 공공기관 고시/공고 목록 페이지의 HTML입니다.
사이트명: {site_name}
URL: {url}

HTML:
{html_trimmed}

이 HTML에서 공고 목록을 크롤링하기 위한 CSS 셀렉터를 찾아주세요.
반드시 아래 JSON 형식으로만 응답하세요. 다른 설명 없이 JSON만 출력하세요:

{{
  "table_body": "공고 목록 테이블/컨테이너의 CSS 셀렉터",
  "title": "제목 요소의 CSS 셀렉터 (table_body 기준 상대경로)",
  "date": "날짜 요소의 CSS 셀렉터 (table_body 기준 상대경로)",
  "reason": "분석 근거 한 줄 설명"
}}"""

    result = call_claude_api(prompt, max_tokens=500)
    parsed = parse_claude_json(result, site_name)
    if parsed:
        logger.info(f"[Claude 셀렉터 분석 완료] {site_name}")
    return parsed


# ─────────────────────────────────────────────
# Zero-Selector: Claude가 HTML 텍스트에서 직접 추출
# ─────────────────────────────────────────────
def zero_selector_extract(site_name: str, html: str, row: dict) -> tuple:
    """CSS 셀렉터 없이 Claude가 텍스트에서 공고 목록 직접 추출"""
    try:
        soup = BeautifulSoup(html, 'html.parser')
        for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'aside']):
            tag.decompose()
        for td in soup.find_all('td'):
            td.insert_after('\t')
        for tr in soup.find_all('tr'):
            tr.insert_after('\n')
        text = soup.get_text(separator='\n', strip=True)
        text = re.sub(r'\n{3,}', '\n\n', text)[:6000]
    except Exception as e:
        return [], [], [], [], f"전처리 오류: {e}"

    if len(text.strip()) < 50:
        return [], [], [], [], "텍스트 추출 실패"

    prompt = f"""다음은 한국 공공기관 고시/공고 목록 페이지의 텍스트입니다.
사이트명: {site_name}

텍스트:
{text}

위 텍스트에서 고시/공고 항목들을 추출하세요.
- 각 항목의 제목(title)과 날짜(date)를 추출
- 날짜는 YYYY-MM-DD 형식으로 정규화
- 날짜 불명 시 "" 표시
- 공고 없으면 빈 배열
- JSON 배열만 출력 (다른 설명 없이)

[{{"title": "공고 제목", "date": "YYYY-MM-DD"}}, ...]"""

    result = call_claude_api(prompt, max_tokens=2000)
    if not result:
        return [], [], [], [], "Claude API 호출 실패"

    try:
        content = result.get('content', [])
        raw = next((b['text'] for b in content if b.get('type') == 'text'), '').strip()
        m = re.search(r'\[.*\]', raw, re.DOTALL)
        if not m:
            return [], [], [], [], "JSON 파싱 실패"
        items = [i for i in json.loads(m.group()) if isinstance(i, dict) and i.get('title')]
    except Exception as e:
        return [], [], [], [], f"결과 파싱 오류: {e}"

    # 품질 검증: 날짜 없는 항목 70% 초과 or 평균 제목 길이 5자 미만 → 신뢰 불가
    if items:
        no_date_ratio = sum(1 for i in items if not i.get('date')) / len(items)
        avg_title_len = sum(len(str(i.get('title', ''))) for i in items) / len(items)
        if no_date_ratio > 0.7 or avg_title_len < 5:
            return [], [], [], [], f"품질 검증 실패 (날짜없음 {no_date_ratio:.0%}, 평균제목 {avg_title_len:.1f}자)"

    titles, dates, collected, unfiltered = [], [], [], []
    url = row.get('URL', '')
    site_no = row.get('SITE_NO', '')
    for item in items:
        title = str(item.get('title', '')).strip()
        date = fix_date_format(str(item.get('date', '')))
        if not title:
            continue
        titles.append(title)
        dates.append(date)
        matched_kw = [kw for kw in FILTER_KEYWORDS if kw in title]
        entry = {"SITE_NO": site_no, "출처": site_name, "URL": url,
                 "제목": title, "작성일": date, "키워드": ", ".join(matched_kw)}
        if matched_kw:
            collected.append(entry)
        else:
            unfiltered.append(entry)

    return titles, dates, collected, unfiltered, ""


# ─────────────────────────────────────────────
# URL 자동 탐색 (Claude 웹서치)
# ─────────────────────────────────────────────
def search_new_url_with_claude(site_name: str, old_url: str) -> str | None:
    """
    Claude 웹서치 툴을 이용해 지자체 고시공고 페이지 새 URL 탐색
    """
    # 도메인 추출 (같은 도메인 내 URL 변경인 경우 힌트로 사용)
    domain_match = re.match(r'(https?://[^/]+)', old_url)
    domain_hint = domain_match.group(1) if domain_match else ''

    prompt = f"""한국 공공기관 고시공고 목록 페이지의 현재 URL을 찾아주세요.

기관명: {site_name}
기존 URL (현재 접근 불가): {old_url}
도메인 힌트: {domain_hint}

웹서치로 현재 접근 가능한 고시공고 목록 페이지 URL을 찾아주세요.
반드시 아래 JSON 형식으로만 응답하세요:

{{
  "new_url": "찾은 URL (없으면 null)",
  "reason": "찾은 근거"
}}"""

    web_search_tool = [{
        "type": "web_search_20250305",
        "name": "web_search"
    }]

    result = call_claude_api(prompt, tools=web_search_tool, max_tokens=1000)
    if not result:
        return None

    # tool_use → text 순으로 응답 파싱
    content_list = result.get('content', [])
    for block in content_list:
        if block.get('type') == 'text':
            text = block.get('text', '').strip()
            text = re.sub(r'```json\s*', '', text)
            text = re.sub(r'```\s*', '', text).strip()
            if '{' in text:
                try:
                    idx_start = text.index('{')
                    idx_end = text.rindex('}') + 1
                    parsed = json.loads(text[idx_start:idx_end])
                    new_url = parsed.get('new_url')
                    if new_url and new_url != 'null' and new_url.startswith('http'):
                        logger.info(f"[URL탐색 성공] {site_name}: {new_url}")
                        return new_url
                except Exception:
                    pass

    logger.warning(f"[URL탐색 실패] {site_name}: 새 URL 찾지 못함")
    return None


# ─────────────────────────────────────────────
# 웹방화벽 우회 시도
# ─────────────────────────────────────────────
def try_bypass_firewall(row) -> tuple:
    """
    다양한 User-Agent와 헤더 조합으로 웹방화벽 우회 시도
    성공 시 (soup, 'ok'), 실패 시 (None, 'firewall_blocked')
    """
    site_name = row['SITE_NAME']
    logger.info(f"[방화벽 우회 시도] {site_name}")

    # 1) User-Agent 로테이션으로 정적 요청 시도
    for i, ua in enumerate(UA_ROTATION):
        headers = {
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "ko-KR,ko;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Referer": re.match(r'(https?://[^/]+)', row['URL']).group(1) if re.match(r'(https?://[^/]+)', row['URL']) else '',
        }
        try:
            time.sleep(2 + i)  # 점진적 대기
            res = requests.get(row['URL'], headers=headers, timeout=(15, 30), verify=False)
            if not is_firewall_blocked(res.text, res.status_code):
                logger.info(f"[방화벽 우회 성공-정적] {site_name} UA#{i+1}")
                return BeautifulSoup(res.text, 'html.parser'), 'ok'
        except Exception:
            pass

    # 2) Selenium UA 로테이션
    for i, ua in enumerate(UA_ROTATION[1:3]):
        try:
            driver = get_driver(timeout=20, ua=ua)
            try:
                driver.get(row['URL'])
            except TimeoutException:
                pass
            time.sleep(10)
            html = driver.page_source
            driver.quit()
            if not is_firewall_blocked(html):
                logger.info(f"[방화벽 우회 성공-동적] {site_name} UA#{i+2}")
                return BeautifulSoup(html, 'html.parser'), 'ok'
        except Exception:
            pass

    logger.warning(f"[방화벽 우회 실패] {site_name}: 모든 시도 실패 (IP 차단으로 판단)")
    return None, 'firewall_blocked'



# ─────────────────────────────────────────────
# 파싱 공통 함수
# ─────────────────────────────────────────────
def parse_soup(soup, row, site_name) -> tuple:
    """
    soup에서 공고 목록 파싱
    반환: (titles_list, dates_list, keyword_matched_list, unfiltered_list, error_msg)
    """
    all_titles, cleaned_dates, collected_data, all_data_unfiltered = [], [], [], []
    try:
        tb = (soup.select(row['table_body'])[1]
              if site_name == '대전광역시고시공고'
              else soup.select_one(row['table_body']))
        if not tb:
            return [], [], [], [], f"테이블 없음: {row['table_body']}"

        try:
            titles = tb.select(row['title'])
            dates = tb.select(row['date'])
        except Exception as e:
            return [], [], [], [], f"셀렉터 오류: {str(e)[:80]}"
        if site_name == '충청도_서천군':
            dates = [d for d in dates if '등록일' not in d.get_text(strip=True)]

        for title, date in zip(titles, dates):
            clean_title = title.get_text(strip=True).replace("\r","").replace("\n","").replace("\t","").strip()
            extracted_date = fix_date_format(extract_date_from_text(date.get_text(separator=" ", strip=True)))
            all_titles.append(clean_title)
            cleaned_dates.append(extracted_date)
            matched_kw = [kw for kw in FILTER_KEYWORDS if kw in clean_title]
            item = {
                "SITE_NO": row['SITE_NO'],
                "출처": site_name,
                "URL": row['URL'],
                "제목": clean_title,
                "작성일": extracted_date,
                "키워드": ", ".join(matched_kw)
            }
            if matched_kw:
                collected_data.append(item)
            else:
                # 키워드 미해당 → 전체공고용 (키워드 컬럼 비워서 구분)
                all_data_unfiltered.append(item)
        return all_titles, cleaned_dates, collected_data, all_data_unfiltered, ""
    except Exception as e:
        return [], [], [], [], f"파싱 오류: {str(e)[:80]}"


# ─────────────────────────────────────────────
# 단일 사이트 크롤링 (자가치유 포함)
# ─────────────────────────────────────────────
def crawl_site(row, gc=None) -> dict:
    site_name = row['SITE_NAME']
    all_titles, cleaned_dates, collected_data, all_unfiltered = [], [], [], []
    page_number = 1
    failed = False
    error_msg = ""
    auto_fixed = False  # 자동복구 성공 여부

    # 연속 실패 자동 스킵
    if site_name in _auto_skip_sites:
        logger.info(f"[자동스킵] {site_name}: 연속 5회 실패")
        return {
            'data': [], 'all_data': [],
            'log': {
                'SITE_NAME': site_name, 'URL': row['URL'],
                'len_tbody': 0, 'unique_date': 0, 'min_date': '', 'max_date': '',
                'status': '자동스킵(연속실패)', 'error_msg': '연속 5회 실패 자동 스킵',
                'auto_fixed': '', '최신제목': '', '최신날짜': '',
            }
        }

    # IP 차단 / fail 사이트 즉시 스킵
    div_val = str(row.get('div', '') or '')
    if 'IP' in div_val or 'fail' in div_val:
        logger.info(f"[스킵] {site_name}: div='{div_val}'")
        return {
            'data': [], 'all_data': [],
            'log': {
                'SITE_NAME': site_name, 'URL': row['URL'],
                'len_tbody': 0, 'unique_date': 0,
                'min_date': '', 'max_date': '',
                'status': f'스킵({div_val})', 'error_msg': '', 'auto_fixed': '',
            '최신제목': '', '최신날짜': '',
            }
        }

    logger.info(f"[시작] {site_name}")

    while page_number <= MAX_PAGES:
        try:
            div = row.get('div', '') or ''
            updated_url = update_url_for_next_page(row['URL'], page_number, div)
            if updated_url is None:
                break
            row = row.copy()
            row['URL'] = updated_url
            row['crawl_type'] = update_crawl_type(
                row['URL'], row['crawl_type'], page_number, row.get('ct2', '')
            )

            soup, status = None, 'ok'
            url_searched = False

            try:
                ct = row['crawl_type']
                if ct == 's':
                    soup, status = static_crawl(row)
                elif ct == 'd':
                    soup, status = dynamic_crawl(row)
                elif ct == 'd1':
                    soup, status = dynamic_crawl_1(row)
                elif ct == 'd2':
                    soup, status = dynamic_crawl_2(row)
                elif ct == 'p':
                    soup, status = post_crawl(row)
                elif ct in CLICK_CRAWL_CONFIG:
                    soup, status = click_dynamic_crawl(row, page_number)
                else:
                    error_msg = f"알 수 없는 타입: {ct}"
                    logger.error(f"[알 수 없는 타입] {site_name}: {ct}")
                    failed = True

                # ── 버튼 없음 → 페이지 끝, 정상 종료 ──
                if status == 'no_button':
                    logger.info(f"[페이지 끝] {site_name}: 페이지 {page_number}에 버튼 없음")
                    break

                # ── 웹방화벽 감지 → 우회 시도 ──
                if not failed and status == 'firewall':
                    soup, status = try_bypass_firewall(row)
                    if status == 'firewall_blocked':
                        error_msg = "웹방화벽 차단 (IP차단, 우회 실패)"
                        failed = True

                # ── HTTP 오류 → URL 탐색 시도 (사이트당 1회) ──
                if not failed and soup is None and (status.startswith('http_') or status == 'error'):
                    url_searched = True
                    error_code = status.split('_')[1] if '_' in status else '?'
                    with _url_search_cache_lock:
                        if site_name in _url_search_cache:
                            new_url = _url_search_cache[site_name]
                            logger.info(f"[URL탐색 캐시 사용] {site_name}: {new_url}")
                        else:
                            logger.warning(f"[HTTP {error_code}] {site_name} → URL 탐색 시도")
                            new_url = search_new_url_with_claude(site_name, row['URL'])
                            _url_search_cache[site_name] = new_url
                    if new_url:
                        row = row.copy()
                        row['URL'] = new_url
                        soup, status = static_crawl(row)
                        if soup is None:
                            soup, status = dynamic_crawl(row)
                        if soup:
                            if gc:
                                update_site_in_sheet(gc, str(row.get('SITE_NO', '')), {'URL': new_url}, original_url=row['URL'])
                            auto_fixed = True
                            logger.info(f"[URL 자동복구 성공] {site_name}: {new_url}")
                        else:
                            error_msg = "URL 복구 후에도 접근 불가 (IP차단 추정)"
                            failed = True
                    else:
                        error_msg = f"HTTP {error_code}: 새 URL 탐색 실패"
                        failed = True

                if not failed and soup is None:
                    error_msg = error_msg or "HTML 수집 실패"
                    failed = True

            except TimeoutException:
                error_msg = "타임아웃"
                logger.warning(f"[타임아웃] {site_name}")
                failed = True
            except Exception as e:
                error_msg = str(e)[:100]
                logger.error(f"[크롤링 오류] {site_name}: {e}")
                failed = True

            if failed:
                break

            # ── 파싱 ──
            titles, dates, data, unfiltered, parse_err = parse_soup(soup, row, site_name)

            if parse_err:
                error_msg = parse_err
                failed = True
                break

            all_titles.extend(titles)
            cleaned_dates.extend(dates)
            collected_data.extend(data)
            all_unfiltered.extend(unfiltered)

            # -- 조기 중단: 이 페이지 최신 공고가 2일 이상 지났으면 다음 페이지 불필요 --
            valid_dates = [d for d in cleaned_dates if re.match(r"20\d{2}-\d{2}-\d{2}", str(d))]
            if valid_dates:
                cutoff_2d = (now_kst() - timedelta(days=2)).strftime('%Y-%m-%d')
                if max(valid_dates) < cutoff_2d:
                    logger.info(f"[조기 중단] {site_name}: 최신 공고({max(valid_dates)})가 2일 이상 지남")
                    break

            if len(set(cleaned_dates)) <= 2:
                page_number += 1
                time.sleep(1)
            else:
                break

        except Exception as e:
            error_msg = f"전체 오류: {str(e)[:80]}"
            logger.error(f"[전체 오류] {site_name}: {e}")
            failed = True
            break

    # ──────────────────────────────────────────
    # 자가치유: 셀렉터 자동수정 + 즉시 재크롤링 (월요일만)
    # ──────────────────────────────────────────
    if failed and not auto_fixed and gc is not None and SELF_HEALING_ENABLED:
        logger.info(f"[자가치유 시작] {site_name}")
        # soup이 있으면 재활용 (CSS 파싱 실패), 없으면 재수집 (HTML 수집 실패)
        html = str(soup) if soup is not None else fetch_html_for_analysis(row)
        if html:
            suggested = analyze_with_claude(site_name, row['URL'], html)
            if suggested and suggested.get('table_body') not in (None, '', 'null', 'Unable to determine'):

                # 새 셀렉터로 즉시 재크롤링 시도
                logger.info(f"[새 셀렉터로 재시도] {site_name}")
                new_row = row.copy()
                new_row['table_body'] = suggested['table_body']
                new_row['title'] = suggested['title']
                new_row['date'] = suggested['date']

                try:
                    ct = new_row['crawl_type']
                    if ct == 's':
                        retry_soup, _ = static_crawl(new_row)
                    elif ct == 'd':
                        retry_soup, _ = dynamic_crawl(new_row)
                    elif ct in CLICK_CRAWL_CONFIG:
                        retry_soup, _ = click_dynamic_crawl(new_row, 1)
                    else:
                        retry_soup = None

                    if retry_soup:
                        titles, dates, data, unfiltered, parse_err = parse_soup(retry_soup, new_row, site_name)
                        if not parse_err and titles:
                            # 재크롤링 성공!
                            all_titles.extend(titles)
                            cleaned_dates.extend(dates)
                            collected_data.extend(data)
                            all_unfiltered.extend(unfiltered)
                            failed = False
                            auto_fixed = True
                            error_msg = ""
                            logger.info(f"[자가치유 성공] {site_name}: {len(titles)}개 공고 수집")
                            # 구글시트 사이트목록 자동 업데이트
                            update_site_in_sheet(gc, str(row.get('SITE_NO', '')), {
                                'table_body': suggested['table_body'],
                                'title': suggested['title'],
                                'date': suggested['date'],
                            }, original_url=row.get('URL', ''))
                        else:
                            logger.warning(f"[자가치유 실패-파싱] {site_name}: {parse_err}")
                    else:
                        logger.warning(f"[자가치유 실패-수집] {site_name}")

                except Exception as e:
                    logger.error(f"[자가치유 오류] {site_name}: {e}")
            else:
                logger.warning(f"[자가치유 불가] {site_name}: Claude가 셀렉터 찾지 못함")

    # ──────────────────────────────────────────
    # Zero-Selector: CSS 실패 또는 0건 추출 시 Claude 직접 추출
    # ──────────────────────────────────────────
    zero_selector_used = False
    if not auto_fixed and (failed or not all_titles):
        logger.info(f"[Zero-Selector 시도] {site_name}")
        # soup이 있으면 재활용, HTML 수집 자체가 실패한 경우 Zero-Selector 스킵
        html_for_zero = str(soup) if soup is not None else None
        if html_for_zero:
            zs_titles, zs_dates, zs_data, zs_unfiltered, zs_err = zero_selector_extract(site_name, html_for_zero, row)
            if not zs_err and zs_titles:
                all_titles.extend(zs_titles)
                cleaned_dates.extend(zs_dates)
                collected_data.extend(zs_data)
                all_unfiltered.extend(zs_unfiltered)
                failed = False
                zero_selector_used = True
                error_msg = ""
                logger.info(f"[Zero-Selector 성공] {site_name}: {len(zs_titles)}건")
            else:
                logger.warning(f"[Zero-Selector 실패] {site_name}: {zs_err or '0건'}")
        else:
            logger.warning(f"[Zero-Selector] {site_name}: HTML 수집 실패")

    final_status = '성공'
    if not failed and zero_selector_used:
        final_status = 'Zero-Selector성공'
    elif not failed and auto_fixed:
        final_status = '자가치유성공'
    elif failed:
        final_status = f'실패({error_msg[:30]})'

    # 최신 공고 샘플 (제목, 날짜)
    latest_title, latest_date = '', ''
    if collected_data:
        latest = max(collected_data, key=lambda x: str(x.get('작성일', '')), default=None)
        if latest:
            latest_title = str(latest.get('제목', ''))[:80]
            latest_date  = str(latest.get('작성일', ''))
    elif all_unfiltered:
        latest = max(all_unfiltered, key=lambda x: str(x.get('작성일', '')), default=None)
        if latest:
            latest_title = str(latest.get('제목', ''))[:80]
            latest_date  = str(latest.get('작성일', ''))

    return {
        'data': collected_data,
        'all_data': all_unfiltered,
        'log': {
            'SITE_NAME': site_name,
            'URL': row['URL'],
            'len_tbody': len(all_titles),
            'unique_date': len(set(cleaned_dates)),
            'min_date': min(cleaned_dates) if cleaned_dates else "",
            'max_date': max(cleaned_dates) if cleaned_dates else "",
            'status': final_status,
            'error_msg': error_msg,
            'auto_fixed': '✅' if auto_fixed else '',
            '최신제목': latest_title,
            '최신날짜': latest_date,
        }
    }


# ─────────────────────────────────────────────
# 병렬 크롤링
# ─────────────────────────────────────────────
def run_crawling_parallel(df, gc, static_workers=15, dynamic_workers=7):
    """정적(requests) 사이트와 동적(Selenium) 사이트를 분리 실행
    - 정적: I/O 바운드 → 15 workers로 빠르게
    - 동적: Chrome 메모리 제약 → 7 workers로 안정적으로
    """
    all_data, all_logs, all_unfiltered = [], [], []

    def collect(futures, desc):
        for future in tqdm(as_completed(futures), total=len(futures), desc=desc):
            try:
                result = future.result()
                all_data.extend(result['data'])
                all_unfiltered.extend(result.get('all_data', []))
                all_logs.append(result['log'])
            except Exception as e:
                logger.error(f"[병렬 오류] {e}")

    df_static  = df[df['crawl_type'] == 's']
    df_dynamic = df[df['crawl_type'] != 's']

    with ThreadPoolExecutor(max_workers=static_workers) as ex:
        collect({ex.submit(crawl_site, row, gc): i for i, row in df_static.iterrows()},
                f"정적 크롤링 ({len(df_static)}개)")

    with ThreadPoolExecutor(max_workers=dynamic_workers) as ex:
        collect({ex.submit(crawl_site, row, gc): i for i, row in df_dynamic.iterrows()},
                f"동적 크롤링 ({len(df_dynamic)}개)")

    return pd.DataFrame(all_data), pd.DataFrame(all_logs), pd.DataFrame(all_unfiltered)


# ─────────────────────────────────────────────
# Google Sheets 업로드
# ─────────────────────────────────────────────
def upload_to_sheet(gc, df, sheet_name, keyword_tab=False):
    if gc is None or df.empty:
        return
    try:
        sh = gc.open_by_key(GOOGLE_SHEET_ID)
        try:
            ws = sh.worksheet(sheet_name)
        except gspread.exceptions.WorksheetNotFound:
            ws = sh.add_worksheet(title=sheet_name, rows="5000", cols="30")

        existing = pd.DataFrame(ws.get_all_records())

        # ── 기존 확인여부/비고 보존 ──
        preserved_meta = {}
        if not existing.empty:
            for _, row in existing.iterrows():
                key = (str(row.get('출처', '')), str(row.get('제목', '')))
                preserved_meta[key] = {
                    '확인여부': row.get('확인여부', ''),
                    '비고':     row.get('비고', ''),
                }

        # ── 신규 데이터와 병합 ──
        combined = pd.concat([existing, df], ignore_index=True) if not existing.empty else df.copy()
        if '출처' in combined.columns and '제목' in combined.columns:
            combined = combined.drop_duplicates(subset=['출처', '제목'], keep='last')

        # ── 키워드 탭 전용 컬럼 추가 ──
        if keyword_tab:
            # 지역
            if '지역' not in combined.columns:
                combined.insert(2, '지역', combined['출처'].apply(extract_region))
            # 경과일
            today_dt = now_kst().date()
            def days_elapsed(d):
                try:
                    return (today_dt - pd.to_datetime(d).date()).days
                except Exception:
                    return ''
            combined['경과일'] = combined['작성일'].apply(days_elapsed)
            # 키워드별 분리
            for kw in FILTER_KEYWORDS:
                combined[kw] = combined.get('키워드', pd.Series(dtype=str)).apply(
                    lambda v: 'Y' if kw in str(v) else ''
                )
            # 확인여부/비고 컬럼
            if '확인여부' not in combined.columns:
                combined['확인여부'] = ''
            if '비고' not in combined.columns:
                combined['비고'] = ''
            # 기존 확인여부/비고 복원
            for idx, row in combined.iterrows():
                key = (str(row.get('출처', '')), str(row.get('제목', '')))
                meta = preserved_meta.get(key, {})
                if meta.get('확인여부'):
                    combined.at[idx, '확인여부'] = meta['확인여부']
                if meta.get('비고'):
                    combined.at[idx, '비고'] = meta['비고']
            # 작성일 내림차순 정렬
            combined['작성일'] = pd.to_datetime(combined['작성일'], errors='coerce')
            combined = combined.sort_values('작성일', ascending=False)
            combined['작성일'] = combined['작성일'].dt.strftime('%Y-%m-%d').fillna('')

        combined = combined.fillna("").astype(str)

        # ── 원문링크 컬럼 (HYPERLINK 수식) ──
        if keyword_tab and 'URL' in combined.columns and '제목' in combined.columns:
            combined['원문링크'] = combined.apply(
                lambda r: f'=HYPERLINK("{r["URL"]}","{r["제목"][:30].replace(chr(34), "")}")'
                if r['URL'].startswith('http') else '', axis=1
            )

        ws.clear()
        ws.update([combined.columns.tolist()] + combined.values.tolist(),
                  value_input_option='USER_ENTERED')
        logger.info(f"[업로드 완료] '{sheet_name}' {len(combined)}행")
    except Exception as e:
        logger.error(f"[업로드 오류] {sheet_name}: {e}")


def upload_log(gc, df, crawled_time: str = ""):
    """크롤링로그 탭에 이력 누적 (30일치 보존)"""
    if gc is None or df.empty:
        return
    try:
        sh = gc.open_by_key(GOOGLE_SHEET_ID)
        try:
            ws = sh.worksheet("크롤링로그")
        except gspread.exceptions.WorksheetNotFound:
            ws = sh.add_worksheet(title="크롤링로그", rows="10000", cols="20")

        df = df.copy()
        df['수집일시'] = crawled_time

        existing = pd.DataFrame(ws.get_all_records())
        combined = pd.concat([existing, df], ignore_index=True) if not existing.empty else df.copy()

        # 30일 초과 이력 제거
        if '수집일시' in combined.columns:
            cutoff = (now_kst() - timedelta(days=30)).strftime('%Y-%m-%d')
            combined = combined[combined['수집일시'].astype(str) >= cutoff]

        combined = combined.fillna("").astype(str)
        ws.clear()
        ws.update([combined.columns.tolist()] + combined.values.tolist())
        logger.info(f"[로그 업로드 완료] {len(combined)}행 (누적)")
    except Exception as e:
        logger.error(f"[로그 업로드 오류]: {e}")


# ─────────────────────────────────────────────
# 키워드 공고 즉시 이메일 알림
# ─────────────────────────────────────────────
def send_keyword_email(df_keyword: pd.DataFrame, crawled_time: str):
    """키워드 매칭 신규 공고를 이메일로 즉시 발송"""
    if not all([EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_RECEIVER]):
        return
    if df_keyword.empty:
        return

    body_lines = [f"[신규 키워드 공고] {crawled_time}\n",
                  f"총 {len(df_keyword)}건 수집됨:\n"]
    for _, row in df_keyword.iterrows():
        body_lines.append(f"  [{row.get('출처','')}] {row.get('제목','')}")
        body_lines.append(f"    작성일: {row.get('작성일','')} | 키워드: {row.get('키워드','')}")
        body_lines.append(f"    링크: {row.get('URL','')}\n")

    body = "\n".join(body_lines)
    try:
        msg = MIMEText(body, 'plain', 'utf-8')
        msg['Subject'] = f"[공고알림] 신규 {len(df_keyword)}건 ({now_kst().strftime('%m/%d %H:%M')} KST)"
        msg['From'] = EMAIL_SENDER
        msg['To'] = EMAIL_RECEIVER
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
            smtp.login(EMAIL_SENDER, EMAIL_PASSWORD)
            smtp.send_message(msg)
        logger.info(f"[키워드 이메일 발송] {len(df_keyword)}건")
    except Exception as e:
        logger.error(f"[키워드 이메일 오류] {e}")


# ─────────────────────────────────────────────
# 대시보드 탭 업로드
# ─────────────────────────────────────────────
def upload_dashboard(gc, df_log: pd.DataFrame, df_keyword: pd.DataFrame, crawled_time: str):
    """대시보드 탭: 수집 현황 요약"""
    if gc is None:
        return
    try:
        sh = gc.open_by_key(GOOGLE_SHEET_ID)
        try:
            ws = sh.worksheet("📊대시보드")
        except gspread.exceptions.WorksheetNotFound:
            ws = sh.add_worksheet(title="📊대시보드", rows="50", cols="10")

        today_str = now_kst().strftime('%Y-%m-%d')

        # 크롤링 현황
        total   = len(df_log)
        success  = len(df_log[df_log['status'] == '성공'])
        healed   = len(df_log[df_log['status'] == '자가치유성공'])
        zero_sel = len(df_log[df_log['status'] == 'Zero-Selector성공'])
        skipped  = len(df_log[df_log['status'].str.contains('스킵', na=False)])
        failed   = len(df_log[df_log['status'].str.startswith('실패', na=False)])
        rate     = f"{round((success+healed+zero_sel)/max(total-skipped,1)*100, 1)}%"

        # 키워드별 집계
        kw_counts = {}
        if not df_keyword.empty and '키워드' in df_keyword.columns:
            for kw in FILTER_KEYWORDS:
                kw_counts[kw] = df_keyword['키워드'].str.contains(kw, na=False).sum()

        # 지역별 집계
        region_counts = {}
        if not df_keyword.empty and '출처' in df_keyword.columns:
            df_keyword['_지역'] = df_keyword['출처'].apply(extract_region)
            region_counts = df_keyword['_지역'].value_counts().to_dict()

        rows = [
            ["📊 공고 수집 대시보드", "", f"기준: {crawled_time}"],
            [""],
            ["▶ 크롤링 현황"],
            ["구분", "건수", ""],
            ["전체 대상", total, ""],
            ["✅ 성공", success, ""],
            ["🤖 Zero-Selector 성공", zero_sel, ""],
            ["🔧 자가치유 성공", healed, ""],
            ["⏭ 스킵(IP차단/연속실패)", skipped, ""],
            ["❌ 실패", failed, ""],
            ["성공률", rate, ""],
            [""],
            ["▶ 오늘 키워드 공고", len(df_keyword), "건"],
            [""],
            ["▶ 키워드별 집계"],
            ["키워드", "건수", ""],
        ]
        for kw, cnt in kw_counts.items():
            rows.append([kw, cnt, ""])
        rows += [[""], ["▶ 지역별 집계"], ["지역", "건수", ""]]
        for region, cnt in sorted(region_counts.items(), key=lambda x: -x[1]):
            rows.append([region, cnt, ""])

        ws.clear()
        ws.update(rows, value_input_option='USER_ENTERED')
        logger.info("[대시보드 업로드 완료]")
    except Exception as e:
        logger.error(f"[대시보드 업로드 오류] {e}")


# ─────────────────────────────────────────────
# 이메일 알림 (자동복구 불가 사이트만)
# ─────────────────────────────────────────────
def send_failure_email(df_log: pd.DataFrame):
    """자동복구도 실패한 사이트 목록을 이메일로 전송"""
    if not all([EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_RECEIVER]):
        return  # 이메일 설정 없으면 스킵

    failed = df_log[df_log['status'].str.startswith('실패', na=False)]
    if failed.empty:
        return

    body_lines = [f"[크롤링 자동복구 실패 알림] {now_kst().strftime('%Y-%m-%d')} (KST)\n"]
    body_lines.append(f"총 {len(failed)}개 사이트 수동 확인 필요:\n")
    for _, row in failed.iterrows():
        body_lines.append(f"  - {row['SITE_NAME']}: {row['error_msg']}")

    body = "\n".join(body_lines)
    try:
        msg = MIMEText(body, 'plain', 'utf-8')
        msg['Subject'] = f"[크롤러] {len(failed)}개 사이트 수동 확인 필요 ({now_kst().strftime('%m/%d')} KST)"
        msg['From'] = EMAIL_SENDER
        msg['To'] = EMAIL_RECEIVER

        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
            smtp.login(EMAIL_SENDER, EMAIL_PASSWORD)
            smtp.send_message(msg)
        logger.info(f"[이메일 알림 발송] 실패 {len(failed)}개 사이트")
    except Exception as e:
        logger.error(f"[이메일 발송 오류] {e}")


# ─────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--test', type=int, metavar='N', default=0,
                        help='테스트 모드: 정적 N개 + 동적 N개만 실행')
    args = parser.parse_args()

    today = now_kst()
    crawled_time = today.strftime('%Y-%m-%d %H:%M:%S KST')
    today_str = today.strftime('%Y%m%d')
    one_day_ago = today - timedelta(days=DAYS_RANGE)

    if args.test:
        logger.info(f"===== [테스트 모드] 정적 {args.test}개 + 동적 {args.test}개 실행 =====")
    else:
        logger.info(f"===== 크롤링 시작 (v4 자가치유): {crawled_time} =====")

    gc = get_gspread_client()
    global _auto_skip_sites
    _auto_skip_sites = load_consecutive_failures(gc)
    if _auto_skip_sites:
        logger.info(f"[자동스킵] {len(_auto_skip_sites)}개 사이트 등록 (연속 5회 실패)")
    df = load_sites(gc)
    logger.info(f"총 {len(df)}개 사이트")

    if args.test:
        n = args.test
        skip_mask = df['div'].astype(str).str.contains('IP|fail', na=False)
        df_static  = df[~skip_mask & (df['crawl_type'] == 's')].head(n)
        df_dynamic = df[~skip_mask & (df['crawl_type'] != 's')].head(n)
        df = pd.concat([df_static, df_dynamic], ignore_index=True)
        logger.info(f"[테스트] 정적 {len(df_static)}개 + 동적 {len(df_dynamic)}개 = {len(df)}개 실행")

    df_fin, df_log, df_all = run_crawling_parallel(df, gc)

    # timezone-aware → naive 변환 (pandas datetime64[us]와 비교를 위해)
    today_naive = today.replace(tzinfo=None)
    one_day_ago_naive = one_day_ago.replace(tzinfo=None)

    # 하루치 필터링 + 중복 제거 (키워드 매칭 공고)
    if not df_fin.empty:
        df_fin['작성일'] = pd.to_datetime(df_fin['작성일'], format='%Y-%m-%d', errors='coerce')
        df_filtered = df_fin[(df_fin['작성일'] >= one_day_ago_naive) & (df_fin['작성일'] <= today_naive)].copy()
        df_filtered = df_filtered.drop_duplicates(subset=['출처', '제목'], keep='last')
        df_filtered['수집일'] = crawled_time
        logger.info(f"필터링 후 {len(df_filtered)}개 공고 (키워드 매칭)")
    else:
        df_filtered = pd.DataFrame()
        logger.warning("수집 데이터 없음")

    # 전체공고 (키워드 미해당) 날짜 필터 + 중복 제거
    if not df_all.empty:
        df_all['작성일'] = pd.to_datetime(df_all['작성일'], format='%Y-%m-%d', errors='coerce')
        df_all_filtered = df_all[(df_all['작성일'] >= one_day_ago_naive) & (df_all['작성일'] <= today_naive)].copy()
        df_all_filtered = df_all_filtered.drop_duplicates(subset=['출처', '제목'], keep='last')
        df_all_filtered['수집일'] = crawled_time
        logger.info(f"전체공고 (키워드 미해당) {len(df_all_filtered)}개")
    else:
        df_all_filtered = pd.DataFrame()

    # 결과 요약 로그
    if not df_log.empty:
        total = len(df_log)
        success  = len(df_log[df_log['status'] == '성공'])
        healed   = len(df_log[df_log['status'] == '자가치유성공'])
        zero_sel = len(df_log[df_log['status'] == 'Zero-Selector성공'])
        failed   = len(df_log[df_log['status'].str.startswith('실패', na=False)])
        skipped  = len(df_log[df_log['status'].str.contains('스킵', na=False)])
        logger.info(f"===== 결과: 성공 {success} | Zero-Selector {zero_sel} | 자가치유 {healed} | 실패 {failed} | 스킵 {skipped} / 전체 {total} =====")

    # 로컬 백업
    df_log.to_excel(f'./df_log_{today_str}.xlsx', index=False)
    if not df_filtered.empty:
        df_filtered.to_excel(f'./df_list_{today_str}.xlsx', index=False)

    # 구글시트 업로드
    upload_to_sheet(gc, df_filtered, "✅키워드공고", keyword_tab=True)
    upload_to_sheet(gc, df_all_filtered, "📋전체공고(키워드제외)")
    upload_log(gc, df_log, crawled_time)
    upload_dashboard(gc, df_log, df_filtered, crawled_time)

    # 이메일 알림
    send_keyword_email(df_filtered, crawled_time)   # 키워드 공고 즉시 발송
    send_failure_email(df_log)                      # 실패 사이트 발송

    logger.info(f"===== 크롤링 완료: {now_kst().strftime('%Y-%m-%d %H:%M:%S')} KST =====")


if __name__ == "__main__":
    main()
