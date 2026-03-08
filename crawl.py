"""
공공기관 고시/공고 자동 크롤러 v5
──────────────────────────────────
Phase 1 (병렬): HTTP/Selenium 크롤링 → 정적 15workers + 동적 4workers
Phase 2 (순차): Claude API → URL탐색 / 자가치유 / Zero-Selector
"""

import argparse
import json
import logging
import math
import os
import re
import smtplib
import sqlite3
import ssl
import time
import threading
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from logging.handlers import TimedRotatingFileHandler

import gspread
import pandas as pd
import requests
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials
from requests.packages.urllib3.exceptions import InsecureRequestWarning
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.common.exceptions import (
    TimeoutException, NoSuchElementException,
    ElementClickInterceptedException, UnexpectedAlertPresentException,
)
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from tqdm import tqdm

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)


# ─────────────────────────────────────────────
# 환경변수 / 상수
# ─────────────────────────────────────────────
KST = timezone(timedelta(hours=9))

GOOGLE_SHEET_ID         = os.environ.get("GOOGLE_SHEET_ID", "")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")
ANTHROPIC_API_KEY       = os.environ.get("ANTHROPIC_API_KEY", "")
EMAIL_SENDER            = os.environ.get("EMAIL_SENDER", "")
EMAIL_PASSWORD          = os.environ.get("EMAIL_PASSWORD", "")
EMAIL_RECEIVER          = os.environ.get("EMAIL_RECEIVER", "")

MAX_PAGES       = 5
DAYS_RANGE      = 1
FILTER_KEYWORDS = ['특허', '제안', '심의', '공법', '실시설계', '보수보강']
RESULT_TTL_DAYS = 30   # GSheets 결과 시트 보관 기간

# SQLite 캐시 (GSheets 폴백용)
DB_PATH = './sites_cache.db'

# 사이트목록에서 제거할 결과성 컬럼 (설정이 아닌 런타임 결과값)
_RESULT_COLS = {'len_tbody', 'unique_date', 'min_date', 'max_date', 'url수집여부'}


REGION_MAP = [
    ('서울', '서울'), ('부산', '부산'), ('대구', '대구'), ('인천', '인천'),
    ('광주', '광주'), ('대전', '대전'), ('울산', '울산'), ('세종', '세종'),
    ('경기', '경기도'), ('강원', '강원도'),
    ('충북', '충청도'), ('충남', '충청도'), ('충청', '충청도'),
    ('전북', '전라도'), ('전남', '전라도'), ('전라', '전라도'),
    ('경북', '경상도'), ('경남', '경상도'), ('경상', '경상도'),
    ('제주', '제주도'),
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}

UA_ROTATION = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

FIREWALL_KEYWORDS = [
    '웹방화벽', 'Web Firewall', 'WAPPLES', 'security policy', '보안정책',
    'Access Denied', '접근이 차단', '차단되었습니다', 'Blocked', 'Forbidden',
    '보안 위반', 'security violation', 'CloudFlare', 'Incapsula',
]

# click 페이징 설정: (selector_type, selector_template, wait_sec)
CLICK_CONFIG = {
    'cd':  ('css',   "body > form > div.default_board > div.paging > table > tbody > tr > td:nth-child(4) > span:nth-child({(page_number-1)*2+1}) > a", 5),
    'cd1': ('css',   "#form1 > div.pgeAbs.mt30 > p > span:nth-child({page_number}) > a", 5),
    'cd2': ('xpath', "/html/body/div[2]/div[2]/div/section[2]/div[1]/form/div[2]/a[{page_number+2}]", 5),
    'cd3': ('css',   "#txt > div.text-center > div > ul > li:nth-child({page_number+2}) > a", 5),
    'cd4': ('css',   "#dataForm > div.pagination.mt-md-4 > a:nth-child({page_number})", 5),
    'cd5': ('css',   "#cont-body > div.paging > div > div > a:nth-child({page_number+2})", 5),
    'cd6': ('css',   "#contentDiv > form > table.MAT10 > tbody > tr > td > table > tbody > tr > td > table > tbody > tr > td:nth-child(4) > span:nth-child({(page_number-1)*2+1}) > a", 5),
    'cd7': ('css',   "#list > div.bod_page > a:nth-child({page_number+2})", 5),
    'cd8': ('css',   "#board > div:nth-child(4) > table > tbody > tr > td > table > tbody > tr > td:nth-child(4) > span:nth-child({(page_number-1)*2+1}) > a", 5),
    'cd9': ('css',   "body > form > div.sb_w > div:nth-child(3) > table > tbody > tr > td > table > tbody > tr > td:nth-child(4) > span:nth-child({(page_number-1)*2+1}) > a", 9),
    'cd10': ('xpath', "/html/body/form/table[3]/tbody/tr/td/table[2]/tbody/tr/td/table/tbody/tr/td/table/tbody/tr/td[4]/span[{(page_number-1)*2+1}]/a", 7),
    'cd11': ('xpath', "//*[@id='list']/div[2]/div/a[{page_number+2}]", 7),
    'cd12': ('css',   "#sidoGosiAPIVO > div.pagination > div.normal_pagination > a:nth-child({page_number+2})", 7),
    'cd13': ('css',   "#txt > div > div.text-center > ul > li:nth-child({page_number+2}) > a", 7),
    'cd14': ('css',   "body > form > div > div > div.p-pagination > div > span.p-page__link-group > a:nth-child({page_number})", 7),
    'cd15': ('css',   "body > form > div > div.paging > table > tbody > tr > td:nth-child(4) > span:nth-child({(page_number-1)*2+1}) > a", 7),
    'cd16': ('css',   "body > form > table > tbody > tr:nth-child(2) > td:nth-child(2) > table > tbody > tr:nth-child(7) > td > table > tbody > tr > td:nth-child({page_number+4}) > a", 7),
    'cd17': ('css',   "body > form > div.board > div > div > table > tbody > tr > td:nth-child(2) > table > tbody > tr > td:nth-child(4) > span:nth-child({(page_number-1)*2+1})", 7),
    'cd18': ('css',   "#contents > div > div.p-wrap.bbs.bbs_list > div.p-pagination > div.p-page_link-group > a:nth-child({page_number})", 7),
    'cd19': ('css',   "#contents > form > table:nth-child(23) > tbody > tr:nth-child(1) > td > table > tbody > tr > td > table > tbody > tr > td:nth-child(4) > span:nth-child({(page_number-1)*2+1}) > a", 7),
    'cd20': ('css',   "body > form > div.pagination > a:nth-child({page_number})", 7),
    'cd21': ('css',   "body > div.pagination > table > tbody > tr > td:nth-child(4) > span:nth-child({(page_number-1)*2+1}) > a", 7),
    'cd22': ('css',   "body > div.pagination > a:nth-child({page_number})", 7),
    'cd23': ('xpath', "/html/body/div[4]/section/div/div/div[2]/div/div/div[3]/div/ul/ul/li[{page_number+2}]", 7),
    'cd24': ('css',   "#content_area > div.container > div > div.content > div.board_list > div.paging > ul > li:nth-child({page_number}) > a", 7),
    'cd25': ('css',   "#eminwonWrap > div.pagination > ul > li:nth-child({page_number}) > a", 7),
    'cd26': ('css',   "#listForm > div.box_page > a:nth-child({page_number+2})", 7),
    'cd27': ('xpath', "/html/body/form/table[2]/tbody/tr/td/table[2]/tbody/tr/td/table/tbody/tr/td/table/tbody/tr/td[4]/span[{(page_number-1)*2+1}]/a", 7),
    'cd28': ('css',   "#A-Contents > div.pager > table > tbody > tr > td > table > tbody > tr > td:nth-child(4) > span:nth-child({(page_number-1)*2+1}) > a", 7),
    'cd29': ('css',   "#contentsArea > div.pager > a:nth-child({page_number+3})", 7),
    'cd30': ('xpath', "/html/body/form/div/table/tbody/tr/td/table/tbody/tr/td/table/tbody/tr/td[4]/span[{(page_number-1)*2+1}]", 7),
    'cd31': ('css',   "body > form > section > div.pager > a:nth-child({page_number+2})", 7),
    'cd32': ('xpath', "/html/body/div/main/div/div/div[2]/div[2]/div[3]/a[{page_number+2}]", 7),
    'cd33': ('css',   "body > form > table:nth-child(12) > tbody > tr > td > table:nth-child(3) > tbody > tr > td > table > tbody > tr > td > table > tbody > tr > td:nth-child(4) > span[{(page_number-1)*2+1}]/a", 7),
    'cd34': ('css',   "#paging-tag > ul > li:nth-child({page_number+2})", 7),
}

# 런타임 로드된 클릭설정 (GSheets/Excel 클릭설정 시트 → 없으면 위 CLICK_CONFIG 사용)
_click_config: dict = {}


def _get_extra(row: dict) -> dict:
    """extra_config JSON 파싱. NaN/빈값/오류 시 빈 dict 반환."""
    raw = row.get('extra_config')
    if is_empty(raw):
        return {}
    raw = str(raw).strip()
    if not raw or raw == 'nan':
        return {}
    try:
        return json.loads(raw)
    except Exception:
        logger.warning(f"[extra_config 파싱 오류] {row.get('SITE_NAME','')}: {raw[:50]}")
    return {}


# ─────────────────────────────────────────────
# 로깅
# ─────────────────────────────────────────────
class _KSTFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=KST)
        return dt.strftime('%Y-%m-%d %H:%M:%S') + f',{int(record.msecs):03d}'

_fmt = _KSTFormatter("%(asctime)s [%(levelname)s] %(message)s")
_fh  = TimedRotatingFileHandler("crawl.log", when="midnight", interval=1,
                                 backupCount=30, encoding="utf-8")
_fh.setFormatter(_fmt)
_sh = logging.StreamHandler()
_sh.setFormatter(_fmt)
logging.basicConfig(level=logging.INFO, handlers=[_fh, _sh])
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# HTTP 세션
# ─────────────────────────────────────────────
_http = requests.Session()
_http.headers.update(HEADERS)
_adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=50, max_retries=0)
_http.mount("http://",  _adapter)
_http.mount("https://", _adapter)

# Claude 동시 호출 제한 (Tier1 50k TPM 기준 Semaphore(2))
_claude_sem      = threading.Semaphore(2)
_url_cache: dict = {}
_url_cache_lock  = threading.Lock()


# ─────────────────────────────────────────────
# 유틸
# ─────────────────────────────────────────────
def now_kst() -> datetime:
    return datetime.now(KST)

def is_self_healing_day() -> bool:
    if os.environ.get("FORCE_SELF_HEALING") == "1":
        return True
    if os.environ.get("DISABLE_SELF_HEALING") == "1":
        return False
    return now_kst().weekday() == 0  # 월요일만

def is_empty(v) -> bool:
    return v is None or v == "" or (isinstance(v, float) and math.isnan(v))

def is_firewall_blocked(html: str, code: int = 200) -> bool:
    if code in (403, 406, 429):
        return True
    return any(kw in (html or "") for kw in FIREWALL_KEYWORDS)

def extract_region(name: str) -> str:
    for key, region in REGION_MAP:
        if key in name:
            return region
    return '기타'


# ─────────────────────────────────────────────
# 날짜 처리
# ─────────────────────────────────────────────
def fix_date(s: str) -> str:
    if not s or not isinstance(s, str):
        return ""
    s = s.strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    if len(s) == 18:
        return f"{s[:4]}-{s[5:7]}-{s[8:10]}"
    s = s.split('~')[0].strip()
    s = re.sub(r'\([A-Za-z]{3}\)', '', s).rstrip('-. ')
    m = re.search(r'(20\d{2}[-./]\d{1,2}[-./]\d{1,2})', s)
    if m:
        return m.group(1).replace('.', '-').replace('/', '-')
    parts = s.split('-')
    if parts and len(parts[0]) == 2:
        return '20' + s
    return "" if not re.search(r'20\d{2}', s) else s

def extract_date(text: str) -> str:
    if not text:
        return ""
    if '공고부서 :' in text:
        return text.split('공고부서 :')[-2].split('등록일 :')[-1].strip()
    if '게재일 :' in text:
        return text.split('게재일 :')[1].strip()
    m = re.search(r'(20\d{2}[-./]\d{1,2}[-./]\d{1,2})', text)
    if m:
        return m.group(1).replace('.', '-').replace('/', '-')
    return text.replace('.', '-').replace('/', '-').replace('등록일 :', '').strip()


# ─────────────────────────────────────────────
# Google Sheets
# ─────────────────────────────────────────────
def get_gc():
    if not GOOGLE_CREDENTIALS_JSON:
        logger.warning("[GSheets] 환경변수 미설정")
        return None
    try:
        scopes = ["https://spreadsheets.google.com/feeds",
                  "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_info(
            json.loads(GOOGLE_CREDENTIALS_JSON), scopes=scopes)
        return gspread.authorize(creds)
    except Exception as e:
        logger.error(f"[GSheets 연결 오류] {e}")
        return None

def load_sites(gc) -> pd.DataFrame:
    def _clean(df: pd.DataFrame) -> pd.DataFrame:
        """결과성 컬럼 제거 + IP차단 사이트 경고"""
        drop_cols = [c for c in df.columns if c in _RESULT_COLS]
        if drop_cols:
            df = df.drop(columns=drop_cols)
        # status 컬럼: 제외하지 않고 경고만 (blocked/fail 사이트도 크롤링 시도)
        if 'status' in df.columns:
            non_active = df[df['status'].astype(str) != 'active']
            if not non_active.empty:
                logger.warning(f"[비활성 사이트 포함] {non_active['SITE_NAME'].tolist()} (blocked/fail → 크롤링 시도)")
        return df

    if gc:
        try:
            df = pd.DataFrame(
                gc.open_by_key(GOOGLE_SHEET_ID).worksheet("사이트목록").get_all_records())
            df = _clean(df)
            logger.info(f"[사이트목록] GSheets {len(df)}개 로드")
            _sync_db(df)   # SQLite 캐시 갱신
            return df
        except Exception as e:
            logger.error(f"[사이트목록 로드 실패] {e} → SQLite 캐시 시도")

    # SQLite 캐시 폴백
    cached = _load_from_db()
    if cached is not None:
        logger.info(f"[사이트목록] SQLite 캐시 {len(cached)}개 사용")
        return cached

    # 최후 폴백: 로컬 Excel (사이트목록 시트 우선, 없으면 첫 시트)
    logger.warning("[사이트목록] 로컬 crawl_test.xlsx 사용 (캐시 없음)")
    xl = pd.ExcelFile('./crawl_test.xlsx')
    sheet = '사이트목록' if '사이트목록' in xl.sheet_names else xl.sheet_names[0]
    return _clean(xl.parse(sheet))


def update_site(gc, site_no: str, updates: dict, original_url: str = None):
    """GSheets + SQLite 캐시 동시 업데이트"""
    # SQLite 캐시 업데이트
    _update_db(site_no, updates)

    if not gc:
        return
    try:
        ws = gc.open_by_key(GOOGLE_SHEET_ID).worksheet("사이트목록")
        records = ws.get_all_records()
        headers = ws.row_values(1)
        for i, rec in enumerate(records, start=2):
            if str(rec.get('SITE_NO', '')) != str(site_no):
                continue
            if original_url and str(rec.get('URL', '')) != original_url:
                continue
            for col, val in updates.items():
                if col in headers:
                    ws.update_cell(i, headers.index(col) + 1, val)
            logger.info(f"[사이트목록 업데이트] {site_no}: {list(updates)}")
            return
    except Exception as e:
        logger.error(f"[사이트목록 업데이트 오류] {e}")


# ─────────────────────────────────────────────
# SQLite 캐시 (GSheets 폴백용)
# ─────────────────────────────────────────────
def _init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS sites (
            SITE_NO TEXT PRIMARY KEY,
            data    TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    con.commit()
    con.close()

def _sync_db(df: pd.DataFrame):
    """GSheets에서 로드한 데이터를 SQLite에 전체 동기화"""
    try:
        _init_db()
        now = now_kst().isoformat()
        con = sqlite3.connect(DB_PATH)
        con.execute("DELETE FROM sites")
        for _, row in df.iterrows():
            con.execute(
                "INSERT OR REPLACE INTO sites (SITE_NO, data, updated_at) VALUES (?, ?, ?)",
                (str(row.get('SITE_NO', '')), json.dumps(row.to_dict(), ensure_ascii=False), now)
            )
        con.commit()
        con.close()
        logger.info(f"[SQLite] {len(df)}개 사이트 캐시 동기화")
    except Exception as e:
        logger.error(f"[SQLite 동기화 오류] {e}")

def _load_from_db() -> pd.DataFrame | None:
    """SQLite 캐시에서 사이트 목록 로드"""
    try:
        _init_db()
        con = sqlite3.connect(DB_PATH)
        rows = con.execute("SELECT data FROM sites").fetchall()
        con.close()
        if not rows:
            return None
        return pd.DataFrame([json.loads(r[0]) for r in rows])
    except Exception as e:
        logger.error(f"[SQLite 로드 오류] {e}")
        return None

def _update_db(site_no: str, updates: dict):
    """SQLite 캐시에서 특정 사이트의 설정값 업데이트"""
    try:
        _init_db()
        con = sqlite3.connect(DB_PATH)
        row = con.execute("SELECT data FROM sites WHERE SITE_NO=?", (site_no,)).fetchone()
        if row:
            data = json.loads(row[0])
            data.update(updates)
            con.execute(
                "UPDATE sites SET data=?, updated_at=? WHERE SITE_NO=?",
                (json.dumps(data, ensure_ascii=False), now_kst().isoformat(), site_no)
            )
            con.commit()
        con.close()
    except Exception as e:
        logger.error(f"[SQLite 업데이트 오류] {e}")


# ─────────────────────────────────────────────
# 클릭설정 로드 (GSheets/Excel → 코드 기본값 순)
# ─────────────────────────────────────────────
def load_click_config(gc) -> dict:
    """클릭설정 시트에서 페이지 클릭 패턴 로드. 없으면 코드 내 CLICK_CONFIG 사용."""
    if gc:
        try:
            ws  = gc.open_by_key(GOOGLE_SHEET_ID).worksheet("클릭설정")
            cfg = {str(r['key']): (str(r['selector_type']), str(r['selector']), int(r['wait_sec']))
                   for r in ws.get_all_records() if r.get('key')}
            if cfg:
                logger.info(f"[클릭설정] GSheets {len(cfg)}개 로드")
                return cfg
        except Exception as e:
            logger.warning(f"[클릭설정 GSheets 로드 실패] {e} → 로컬 시도")

    try:
        xl = pd.ExcelFile('./crawl_test.xlsx')
        if '클릭설정' in xl.sheet_names:
            df_cc = xl.parse('클릭설정')
            cfg   = {str(r['key']): (str(r['selector_type']), str(r['selector']), int(r['wait_sec']))
                     for _, r in df_cc.iterrows() if r.get('key')}
            if cfg:
                logger.info(f"[클릭설정] Excel {len(cfg)}개 로드")
                return cfg
    except Exception as e:
        logger.warning(f"[클릭설정 Excel 로드 실패] {e}")

    logger.info("[클릭설정] 코드 내 기본값 사용")
    return CLICK_CONFIG


# ─────────────────────────────────────────────
# Chrome 드라이버 (컨텍스트 매니저)
# ─────────────────────────────────────────────
_SESSION_ERRORS = ('invalid session id', 'session not created', 'session deleted',
                   'chrome not reachable', 'DevToolsActivePort', 'disconnected')

def _is_session_error(e: Exception) -> bool:
    return any(k in str(e) for k in _SESSION_ERRORS)

def _build_driver(timeout: int = 20, ua: str = None):
    opts = Options()
    for arg in ["--headless", "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                "--window-size=1920,1080", "--remote-debugging-port=0", "--disable-extensions",
                "--no-first-run", "--disable-default-apps", "--disable-background-networking",
                "--disable-sync"]:
        opts.add_argument(arg)
    opts.add_experimental_option("prefs", {
        "profile.managed_default_content_settings.images": 2,
        "profile.default_content_setting_values.notifications": 2,
    })
    if ua:
        opts.add_argument(f"--user-agent={ua}")
    for attempt in range(2):
        try:
            d = webdriver.Chrome(options=opts)
            d.set_page_load_timeout(timeout)
            return d
        except Exception as e:
            if attempt == 0:
                logger.warning(f"[Chrome 시작 실패] 재시도... ({e})")
                time.sleep(2)
            else:
                raise

@contextmanager
def chrome_driver(timeout: int = 20, ua: str = None):
    driver = None
    try:
        driver = _build_driver(timeout, ua)
        yield driver
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


# ─────────────────────────────────────────────
# 페이지 URL / 크롤링 타입 업데이트
# ─────────────────────────────────────────────
_URL_PATTERNS = {
    "pageIndex=": lambda u, p: re.sub(r"pageIndex=\d+", f"pageIndex={p}", u),
    "page=":      lambda u, p: re.sub(r"page=\d+",      f"page={p}",      u),
    "Page=":      lambda u, p: re.sub(r"Page=\d+",      f"Page={p}",      u),
    "&cpn=":      lambda u, p: re.sub(r"&cpn=\d+",      f"&cpn={p}",      u),
    "pageNo=":    lambda u, p: re.sub(r"pageNo=\d+",    f"pageNo={p}",    u),
    "offset=":    lambda u, p: re.sub(r"offset=\d+",    f"offset={(p-1)*15}", u),
    "?p=":        lambda u, p: re.sub(r"\?p=\d+",       f"?p={p}",        u),
    "Page2=":     lambda u, p: re.sub(r"Page2=\d+",     f"Page2={p}",     u),
    "Start=":     lambda u, p: re.sub(r"Start=\d+",     f"Start={(p-1)*10}", u),
    "pageid=":    lambda u, p: re.sub(r"pageid=\d+",    f"pageid={p}",    u),
}

def page_url(url: str, page: int, row: dict) -> str | None:
    """다음 페이지 URL 계산.
    None = 더 이상 페이지 없음 (page_type=none).
    page_type=click → URL 그대로 (Selenium 클릭이 이동 담당).
    page_type=url_param → URL 쿼리스트링 패턴 치환.
    """
    if page == 1:
        return url
    pt = str(row.get('page_type') or '').lower()
    if pt == 'click':
        return url                          # Selenium 클릭으로 이동, URL 불변
    if pt == 'url_param':
        for pat, fn in _URL_PATTERNS.items():
            if pat in url:
                return fn(url, page)
        return url                          # 패턴 없어도 그대로 반환
    return None                             # page_type=none → 단일 페이지

def _resolve_selector(tmpl: str, page: int) -> str:
    """셀렉터 템플릿의 {expr} 패턴을 page_number 기준으로 eval 치환
    예: {page_number+2} → 4  (page=2일 때)
    """
    def _eval(m):
        try:
            return str(eval(m.group(1), {"page_number": page, "__builtins__": {}}))
        except Exception:
            return m.group(0)
    return re.sub(r'\{([^}]+)\}', _eval, tmpl)


# ─────────────────────────────────────────────
# Fetchers
# ─────────────────────────────────────────────
def _fetch_static(row: dict) -> tuple:
    """정적 HTTP 크롤링 (SSL 폴백 포함)"""
    try:
        res = _http.get(row['URL'], timeout=(10, 30), verify=False)
        res.raise_for_status()
        res.encoding = 'utf-8'
        if is_firewall_blocked(res.text, res.status_code):
            return None, 'firewall'
        return BeautifulSoup(res.text, 'html.parser'), 'ok'
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response else 0
        logger.error(f"[정적 HTTP오류] {row['SITE_NAME']}: {code}")
        return None, f'http_{code}'
    except requests.exceptions.SSLError:
        # SSL 낮은 보안 설정으로 폴백
        try:
            ctx = ssl.create_default_context()
            ctx.set_ciphers("DEFAULT@SECLEVEL=1")
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            if hasattr(ssl, 'OP_LEGACY_SERVER_CONNECT'):
                ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
            from requests.adapters import HTTPAdapter
            class _SSLAdapter(HTTPAdapter):
                def init_poolmanager(self, *args, **kwargs):
                    kwargs['ssl_context'] = ctx
                    super().init_poolmanager(*args, **kwargs)
            s = requests.Session()
            s.mount("https://", _SSLAdapter())
            res = s.get(row['URL'], timeout=(10, 30), verify=False)
            res.raise_for_status()
            res.encoding = 'utf-8'
            if is_firewall_blocked(res.text, res.status_code):
                return None, 'firewall'
            logger.info(f"[SSL폴백 성공] {row['SITE_NAME']}")
            return BeautifulSoup(res.text, 'html.parser'), 'ok'
        except Exception as e2:
            logger.error(f"[정적 크롤링 오류] {row['SITE_NAME']}: {e2}")
            return None, 'error'
    except Exception as e:
        logger.error(f"[정적 크롤링 오류] {row['SITE_NAME']}: {e}")
        return None, 'error'


def _fetch_dynamic(row: dict) -> tuple:
    """동적 Selenium 크롤링 (fetch_type=selenium, 2회 재시도).
    extra_config:
      pre_click   → 드롭다운 등 옵션 선택 후 콘텐츠 로드 (구 d1 동작)
      click_button → CSS 셀렉터 버튼 클릭 후 콘텐츠 로드 (구 d2 동작)
      (없음)      → 단순 대기 후 page_source 수집 (구 d 동작)
    """
    name  = row['SITE_NAME']
    extra = _get_extra(row)
    for attempt in range(2):
        try:
            with chrome_driver(timeout=30) as driver:
                try:
                    driver.get(row['URL'])
                except TimeoutException:
                    logger.warning(f"[로딩 타임아웃] {name}")
                except UnexpectedAlertPresentException:
                    try:
                        driver.switch_to.alert.accept()
                    except Exception:
                        pass

                pre_click  = extra.get('pre_click')
                click_btn  = extra.get('click_button')
                if pre_click:
                    time.sleep(7)
                    driver.find_element(By.ID,    pre_click.get('id',    'ofr_pageSize')).click()
                    driver.find_element(By.XPATH, pre_click.get('xpath', '//*[@id="ofr_pageSize"]/option[1]')).click()
                    time.sleep(3)
                elif click_btn:
                    time.sleep(7)
                    driver.find_element(By.CSS_SELECTOR, click_btn).click()
                    time.sleep(3)
                else:
                    time.sleep(extra.get('sleep', 10))

                # page_source 호출 시 alert 처리
                try:
                    html = driver.page_source
                except UnexpectedAlertPresentException:
                    try:
                        driver.switch_to.alert.accept()
                    except Exception:
                        pass
                    html = driver.page_source

                if is_firewall_blocked(html):
                    return None, 'firewall'
                return BeautifulSoup(html, 'html.parser'), 'ok'

        except UnexpectedAlertPresentException:
            if attempt == 0:
                logger.warning(f"[Alert 팝업 재시도] {name}")
                continue
            return None, 'error'
        except Exception as e:
            if attempt == 0 and _is_session_error(e):
                logger.warning(f"[세션 오류 재시도] {name}")
                time.sleep(3)
                continue
            logger.error(f"[동적 크롤링 오류] {name}: {e}")
            return None, 'error'
    return None, 'error'


def _fetch_post(row: dict) -> tuple:
    """POST 크롤링"""
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
        'nodate_last_mm': '', 'not_ancmt_reg_no': '', 'Key': 'B_Subject', 'temp': '',
    }
    try:
        res = _http.post(row['URL'], data=data, timeout=(10, 30))
        return BeautifulSoup(res.content.decode('utf-8-sig'), 'html.parser'), 'ok'
    except Exception as e:
        logger.error(f"[POST 크롤링 오류] {row['SITE_NAME']}: {e}")
        return None, 'error'


def _fetch_click(row: dict, page: int) -> tuple:
    """클릭 기반 페이지 이동 크롤링 (page_type=click, page_config=cd7 등).
    _click_config(GSheets/Excel 클릭설정 시트)에서 셀렉터를 조회한다.
    """
    pc   = str(row.get('page_config') or '')
    conf = _click_config.get(pc)
    if not conf:
        return None, f'unknown_page_config:{pc}'
    sel_type, sel_tmpl, wait = conf
    selector = _resolve_selector(sel_tmpl, page)
    name = row['SITE_NAME']
    for attempt in range(2):
        try:
            with chrome_driver() as driver:
                driver.get(row['URL'])
                time.sleep(wait)
                if page > 1:
                    by = By.CSS_SELECTOR if sel_type == 'css' else By.XPATH
                    try:
                        btn = driver.find_element(by, selector)
                        driver.execute_script("arguments[0].scrollIntoView(true);", btn)
                        time.sleep(0.5)
                        try:
                            btn.click()
                        except (ElementClickInterceptedException, Exception):
                            driver.execute_script("arguments[0].click();", btn)
                        time.sleep(wait)
                    except NoSuchElementException:
                        return None, 'no_button'
                html = driver.page_source
                if is_firewall_blocked(html):
                    return None, 'firewall'
                return BeautifulSoup(html, 'html.parser'), 'ok'
        except Exception as e:
            if attempt == 0 and _is_session_error(e):
                logger.warning(f"[세션 오류 재시도] {name}")
                time.sleep(3)
                continue
            logger.error(f"[클릭 크롤링 오류] {name}: {e}")
            return None, 'error'
    return None, 'error'


def fetch(row: dict, page: int = 1) -> tuple:
    """fetch_type / page_type 기반으로 fetcher 선택.

    page_type=click + page>1  → Selenium 클릭 페이지 이동
    fetch_type=http           → 정적 HTTP (extra_config.method=post 이면 POST)
    fetch_type=selenium       → 동적 Selenium
    """
    ft    = str(row.get('fetch_type') or 'http').lower()
    pt    = str(row.get('page_type')  or 'none').lower()
    extra = _get_extra(row)

    if pt == 'click' and page > 1:
        return _fetch_click(row, page)

    if ft == 'http':
        if extra.get('method') == 'post':
            return _fetch_post(row)
        return _fetch_static(row)

    if ft == 'selenium':
        return _fetch_dynamic(row)

    return None, f'unknown_fetch_type:{ft}'


def try_bypass_firewall(row: dict) -> tuple:
    """UA 로테이션으로 웹방화벽 우회"""
    name = row['SITE_NAME']
    logger.info(f"[방화벽 우회 시도] {name}")
    domain = re.match(r'(https?://[^/]+)', row['URL'])
    referer = domain.group(1) if domain else ''

    for i, ua in enumerate(UA_ROTATION):
        try:
            time.sleep(2 + i)
            h = {**HEADERS, "User-Agent": ua, "Referer": referer,
                 "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                 "Accept-Language": "ko-KR,ko;q=0.9", "Accept-Encoding": "gzip, deflate, br"}
            res = _http.get(row['URL'], headers=h, timeout=(15, 30), verify=False)
            if not is_firewall_blocked(res.text, res.status_code):
                logger.info(f"[방화벽 우회 성공-정적] {name} UA#{i+1}")
                return BeautifulSoup(res.text, 'html.parser'), 'ok'
        except Exception:
            pass

    for ua in UA_ROTATION[1:3]:
        try:
            with chrome_driver(ua=ua) as driver:
                try:
                    driver.get(row['URL'])
                except TimeoutException:
                    pass
                time.sleep(10)
                html = driver.page_source
                if not is_firewall_blocked(html):
                    return BeautifulSoup(html, 'html.parser'), 'ok'
        except Exception:
            pass

    logger.warning(f"[방화벽 우회 실패] {name}")
    return None, 'firewall_blocked'


# ─────────────────────────────────────────────
# 파서
# ─────────────────────────────────────────────
def parse_soup(soup: BeautifulSoup, row: dict, site_name: str) -> tuple:
    """soup에서 공고 목록 추출
    반환: (titles, dates, keyword_data, all_data, error_msg)
    """
    titles, dates, keyword_data, all_data = [], [], [], []
    try:
        # extra_config: _get_extra()로 파싱 (오류 시 빈 dict)
        extra = _get_extra(row)

        # tbody_index: 특정 인덱스의 tbody 사용 (기본 0 = select_one)
        tbody_index = extra.get('tbody_index', 0)
        if tbody_index:
            tbs = soup.select(row['table_body'])
            tb  = tbs[tbody_index] if len(tbs) > tbody_index else None
        else:
            tb = soup.select_one(row['table_body'])

        if not tb:
            return [], [], [], [], f"테이블 없음: {row['table_body']}"

        title_els = tb.select(row['title'])
        date_els  = tb.select(row['date'])

        # date_exclude_text: 특정 텍스트 포함 날짜 요소 제거
        exclude_text = extra.get('date_exclude_text')
        if exclude_text:
            date_els = [d for d in date_els if exclude_text not in d.get_text(strip=True)]

        for t_el, d_el in zip(title_els, date_els):
            title = t_el.get_text(strip=True).replace("\r", "").replace("\n", "").replace("\t", "").strip()
            date  = fix_date(extract_date(d_el.get_text(separator=" ", strip=True)))
            titles.append(title)
            dates.append(date)
            matched = [kw for kw in FILTER_KEYWORDS if kw in title]
            item = {
                "SITE_NO": row['SITE_NO'], "출처": site_name, "URL": row['URL'],
                "제목": title, "작성일": date, "키워드": ", ".join(matched),
            }
            (keyword_data if matched else all_data).append(item)

        return titles, dates, keyword_data, all_data, ""
    except Exception as e:
        return [], [], [], [], f"파싱 오류: {str(e)[:80]}"


# ─────────────────────────────────────────────
# Claude API
# ─────────────────────────────────────────────
def call_claude(prompt: str, tools: list = None, max_tokens: int = 1000) -> dict | None:
    if not ANTHROPIC_API_KEY:
        return None
    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if tools:
        payload["tools"] = tools
    with _claude_sem:
        try:
            res = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_API_KEY,
                         "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json=payload, timeout=60,
            )
            if res.status_code == 429:
                logger.warning("[Claude] 429 rate limit")
                return None
            if res.status_code != 200:
                logger.error(f"[Claude] HTTP {res.status_code}")
                return None
            time.sleep(3)  # 연속 호출 방지
            return res.json()
        except Exception as e:
            logger.error(f"[Claude 오류] {e}")
            return None

def _extract_claude_text(result: dict) -> str:
    return next((b['text'] for b in result.get('content', [])
                 if b.get('type') == 'text'), '').strip()

def _parse_claude_json(result: dict) -> dict | None:
    text = _extract_claude_text(result)
    text = re.sub(r'```(?:json)?\s*', '', text).strip()
    if text.startswith('{'):
        try:
            return json.loads(text)
        except Exception:
            pass
    return None


def search_new_url(site_name: str, old_url: str) -> str | None:
    """Claude 웹서치로 새 고시공고 URL 탐색"""
    domain = re.match(r'(https?://[^/]+)', old_url)
    prompt = f"""한국 공공기관 고시공고 목록 페이지의 현재 URL을 찾아주세요.
기관명: {site_name}
기존 URL (현재 접근 불가): {old_url}
도메인 힌트: {domain.group(1) if domain else ''}

웹서치로 현재 접근 가능한 고시공고 목록 URL을 찾아주세요.
JSON으로만 응답: {{"new_url": "찾은 URL (없으면 null)", "reason": "근거"}}"""

    result = call_claude(prompt,
                         tools=[{"type": "web_search_20250305", "name": "web_search"}],
                         max_tokens=1000)
    if not result:
        return None
    text = _extract_claude_text(result)
    m = re.search(r'\{.*\}', text, re.DOTALL)
    if m:
        try:
            parsed = json.loads(m.group())
            url = parsed.get('new_url')
            if url and url != 'null' and url.startswith('http'):
                logger.info(f"[URL탐색 성공] {site_name}: {url}")
                return url
        except Exception:
            pass
    logger.warning(f"[URL탐색 실패] {site_name}")
    return None


def analyze_selectors(site_name: str, url: str, html: str) -> dict | None:
    """HTML에서 CSS 셀렉터 자동 분석"""
    try:
        s = BeautifulSoup(html, 'html.parser')
        for tag in s(['script', 'style', 'link', 'meta']):
            tag.decompose()
        body = s.find('body')
        trimmed = str(body)[:20000] if body else html[:20000]
    except Exception:
        trimmed = html[:20000]

    prompt = f"""아래는 공공기관 고시/공고 목록 페이지의 HTML입니다.
사이트명: {site_name} | URL: {url}

HTML:
{trimmed}

공고 목록 크롤링용 CSS 셀렉터를 JSON으로만 응답하세요 (설명 없이):
{{"table_body": "컨테이너 셀렉터", "title": "제목 셀렉터(상대)", "date": "날짜 셀렉터(상대)", "reason": "분석 근거"}}"""

    result = call_claude(prompt, max_tokens=500)
    return _parse_claude_json(result) if result else None


def zero_selector(site_name: str, html: str, row: dict) -> tuple:
    """CSS 셀렉터 없이 Claude가 텍스트에서 직접 공고 추출"""
    try:
        s = BeautifulSoup(html, 'html.parser')
        for tag in s(['script', 'style', 'nav', 'footer', 'header', 'aside']):
            tag.decompose()
        for td in s.find_all('td'):
            td.insert_after('\t')
        for tr in s.find_all('tr'):
            tr.insert_after('\n')
        text = re.sub(r'\n{3,}', '\n\n', s.get_text(separator='\n', strip=True))[:6000]
    except Exception as e:
        return [], [], [], [], f"전처리 오류: {e}"

    if len(text.strip()) < 50:
        return [], [], [], [], "텍스트 너무 짧음"

    prompt = f"""다음은 한국 공공기관 고시/공고 목록 페이지 텍스트입니다.
사이트명: {site_name}

{text}

고시/공고 항목을 추출하세요.
- 날짜는 YYYY-MM-DD 형식으로 정규화, 불명확하면 ""
- JSON 배열만 출력 (설명 없이):
[{{"title": "공고 제목", "date": "YYYY-MM-DD"}}, ...]"""

    result = call_claude(prompt, max_tokens=2000)
    if not result:
        return [], [], [], [], "Claude API 호출 실패"

    try:
        raw = _extract_claude_text(result)
        m   = re.search(r'\[.*\]', raw, re.DOTALL)
        if not m:
            return [], [], [], [], "JSON 파싱 실패"
        items = [i for i in json.loads(m.group()) if isinstance(i, dict) and i.get('title')]
    except Exception as e:
        return [], [], [], [], f"결과 파싱 오류: {e}"

    if not items:
        return [], [], [], [], "0건"

    # 품질 검증: 날짜 없는 항목 70% 초과 or 평균 제목 5자 미만
    no_date = sum(1 for i in items if not i.get('date')) / len(items)
    avg_len = sum(len(str(i.get('title', ''))) for i in items) / len(items)
    if no_date > 0.7 or avg_len < 5:
        return [], [], [], [], f"품질 검증 실패 (날짜없음 {no_date:.0%}, 평균제목 {avg_len:.1f}자)"

    titles, dates, keyword_data, all_data = [], [], [], []
    for item in items:
        title = str(item.get('title', '')).strip()
        date  = fix_date(str(item.get('date', '')))
        if not title:
            continue
        titles.append(title)
        dates.append(date)
        matched = [kw for kw in FILTER_KEYWORDS if kw in title]
        entry = {"SITE_NO": row.get('SITE_NO', ''), "출처": site_name,
                 "URL": row.get('URL', ''), "제목": title, "작성일": date,
                 "키워드": ", ".join(matched)}
        (keyword_data if matched else all_data).append(entry)

    return titles, dates, keyword_data, all_data, ""


# ─────────────────────────────────────────────
# 결과 빌더 (crawl_site / process_with_claude 공용)
# ─────────────────────────────────────────────
def _build_result(site_name: str, row: dict,
                  titles: list, dates: list, kw_data: list, all_data: list,
                  failed: bool, auto_fixed: bool, zero_used: bool,
                  error_msg: str) -> dict:
    if not failed and zero_used:
        status = 'Zero-Selector성공'
    elif not failed and auto_fixed:
        status = '자가치유성공'
    elif failed:
        status = f'실패({error_msg[:30]})'
    else:
        status = '성공'

    latest_title, latest_date = '', ''
    if not failed:
        pool = [x for x in kw_data + all_data if x.get('제목')]
        if pool:
            best = max(pool, key=lambda x: str(x.get('작성일', '')))
            latest_title = str(best.get('제목', ''))[:80]
            latest_date  = str(best.get('작성일', ''))

    return {
        'data':     kw_data,
        'all_data': all_data,
        'log': {
            'SITE_NAME':   site_name,
            'URL':         row['URL'],
            'len_tbody':   len(titles),
            'unique_date': len(set(dates)),
            'min_date':    min(dates) if dates else '',
            'max_date':    max(dates) if dates else '',
            'status':      status,
            'error_msg':   error_msg,
            'auto_fixed':  '✅' if auto_fixed else '',
            '최신제목':     latest_title,
            '최신날짜':     latest_date,
        },
    }


# ─────────────────────────────────────────────
# Phase 2: Claude 처리
# ─────────────────────────────────────────────
def process_with_claude(pending: dict, gc) -> dict:
    site_name  = pending['site_name']
    row        = pending['row']
    soup       = pending['soup']
    failed     = pending['failed']
    auto_fixed = pending['auto_fixed']
    error_msg  = pending['error_msg']
    titles     = list(pending['all_titles'])
    dates      = list(pending['cleaned_dates'])
    kw_data    = list(pending['collected_data'])
    all_data   = list(pending['all_unfiltered'])
    url_search = pending.get('url_search')
    zero_used  = False

    # ── Phase A: URL 탐색 ──
    if url_search and failed:
        with _url_cache_lock:
            if site_name in _url_cache:
                new_url = _url_cache[site_name]
                logger.info(f"[URL탐색 캐시] {site_name}")
            else:
                logger.warning(f"[HTTP {url_search['error_code']}] {site_name} → URL 탐색")
                new_url = search_new_url(site_name, url_search['original_url'])
                _url_cache[site_name] = new_url

        if new_url:
            new_row = {**row, 'URL': new_url}
            retry_soup, _ = _fetch_static(new_row)
            if retry_soup is None:
                retry_soup, _ = _fetch_dynamic(new_row, 'd')
            if retry_soup:
                t, d, kd, ad, err = parse_soup(retry_soup, new_row, site_name)
                if not err:
                    titles.extend(t); dates.extend(d)
                    kw_data.extend(kd); all_data.extend(ad)
                    failed = False; auto_fixed = True; error_msg = ""
                    soup = retry_soup; row = new_row
                    update_site(gc, str(row.get('SITE_NO', '')),
                                {'URL': new_url}, original_url=url_search['original_url'])
                    logger.info(f"[URL 자동복구 성공] {site_name}: {new_url}")
                else:
                    failed = True; error_msg = f"URL 복구 후 파싱 실패: {err}"
            else:
                failed = True; error_msg = "URL 복구 후 접근 불가"
        else:
            failed = True; error_msg = f"HTTP {url_search['error_code']}: 새 URL 탐색 실패"

    # ── Phase B: 자가치유 (월요일만) ──
    if failed and not auto_fixed and gc and is_self_healing_day():
        logger.info(f"[자가치유 시작] {site_name}")
        html = str(soup) if soup else None
        if not html:
            try:
                with chrome_driver() as driver:
                    try:
                        driver.get(row['URL'])
                    except TimeoutException:
                        pass
                    time.sleep(6)
                    html = driver.page_source
            except Exception:
                pass

        if html:
            suggested = analyze_selectors(site_name, row['URL'], html)
            if suggested and suggested.get('table_body') not in (None, '', 'null', 'Unable to determine'):
                new_row = {**row, 'table_body': suggested['table_body'],
                           'title': suggested['title'], 'date': suggested['date']}
                # 기존 soup으로 먼저 시도 (재수집 비용 절감)
                t, d, kd, ad, err = parse_soup(soup, new_row, site_name)
                if err or not t:
                    retry_soup, _ = fetch(new_row, 1)
                    if retry_soup:
                        t, d, kd, ad, err = parse_soup(retry_soup, new_row, site_name)
                if not err and t:
                    titles.extend(t); dates.extend(d)
                    kw_data.extend(kd); all_data.extend(ad)
                    failed = False; auto_fixed = True; error_msg = ""
                    update_site(gc, str(row.get('SITE_NO', '')),
                                {k: suggested[k] for k in ('table_body', 'title', 'date')},
                                original_url=row.get('URL', ''))
                    logger.info(f"[자가치유 성공] {site_name}")
                else:
                    logger.warning(f"[자가치유 실패] {site_name}: {err}")
            else:
                logger.warning(f"[자가치유 불가] {site_name}: 셀렉터 찾지 못함")

    # ── Phase C: Zero-Selector ──
    if not auto_fixed and failed:
        html_for_zs = str(soup) if soup else None
        if html_for_zs:
            logger.info(f"[Zero-Selector 시도] {site_name}")
            t, d, kd, ad, err = zero_selector(site_name, html_for_zs, row)
            if not err and t:
                titles.extend(t); dates.extend(d)
                kw_data.extend(kd); all_data.extend(ad)
                failed = False; zero_used = True; error_msg = ""
                logger.info(f"[Zero-Selector 성공] {site_name}: {len(t)}건")
            else:
                logger.warning(f"[Zero-Selector 실패] {site_name}: {err or '0건'}")
        else:
            logger.warning(f"[Zero-Selector] {site_name}: HTML 없음")

    return _build_result(site_name, row, titles, dates, kw_data, all_data,
                         failed, auto_fixed, zero_used, error_msg)


# ─────────────────────────────────────────────
# 단일 사이트 크롤링
# ─────────────────────────────────────────────
def crawl_site(row) -> dict:
    row       = dict(row)   # pandas Series → dict
    site_name = row['SITE_NAME']
    titles, dates, kw_data, all_data = [], [], [], []
    failed = False; auto_fixed = False; error_msg = ""
    soup = None; page = 1; pending_url = None

    logger.info(f"[시작] {site_name}")

    while page <= MAX_PAGES:
        try:
            url = page_url(row['URL'], page, row)
            if url is None:
                break
            row = {**row, 'URL': url}

            soup, status = fetch(row, page)

            if status == 'no_button':
                break
            if status == 'firewall':
                soup, status = try_bypass_firewall(row)
                if status == 'firewall_blocked':
                    failed = True; error_msg = "웹방화벽 차단 (우회 실패)"
            if soup is None and (status.startswith('http_') or status == 'error'):
                code = status.split('_')[1] if '_' in status else '?'
                pending_url = {'error_code': code, 'original_url': row['URL']}
                failed = True; error_msg = f"HTTP {code}: URL탐색 대기"
            if soup is None and not failed:
                failed = True; error_msg = error_msg or "HTML 수집 실패"

        except TimeoutException:
            failed = True; error_msg = "타임아웃"
            logger.warning(f"[타임아웃] {site_name}")
        except Exception as e:
            failed = True; error_msg = str(e)[:100]
            logger.error(f"[크롤링 오류] {site_name}: {e}")

        if failed:
            break

        t, d, kd, ad, err = parse_soup(soup, row, site_name)
        if err:
            failed = True; error_msg = err; break

        titles.extend(t); dates.extend(d); kw_data.extend(kd); all_data.extend(ad)

        # 날짜 기반 다음 페이지 판단 (이번 페이지 날짜만 사용, DAYS_RANGE 기준)
        this_page_valid = sorted([x for x in d if re.match(r"20\d{2}-\d{2}-\d{2}", str(x))])
        cutoff_str = (now_kst() - timedelta(days=DAYS_RANGE)).strftime('%Y-%m-%d')

        if not this_page_valid:
            break  # 날짜 파싱 불가 → 중단

        if max(this_page_valid) < cutoff_str:
            # 이 페이지 최신조차 수집 기간 밖 → 조기 중단
            logger.info(f"[조기 중단] {site_name}: 최신({max(this_page_valid)}) < {cutoff_str}")
            break

        if min(this_page_valid) < cutoff_str:
            # 수집 기간 내 데이터 있고, 그 이전도 있음 → 경계 페이지, 다음 불필요
            break

        # 모두 수집 기간 내 → 다음 페이지에 더 있을 수 있음
        page += 1
        time.sleep(1)

    # Claude 처리 위임 판단
    needs_healing  = failed and not auto_fixed and is_self_healing_day()
    needs_zero     = not auto_fixed and failed and soup is not None
    if pending_url or needs_healing or needs_zero:
        return {
            'data': kw_data, 'all_data': all_data,
            'log': {
                'SITE_NAME': site_name, 'URL': row['URL'],
                'len_tbody': 0, 'unique_date': 0, 'min_date': '', 'max_date': '',
                'status': 'Claude대기', 'error_msg': error_msg, 'auto_fixed': '',
                '최신제목': '', '최신날짜': '',
            },
            'pending': {
                'site_name': site_name, 'row': row, 'soup': soup,
                'failed': failed, 'auto_fixed': auto_fixed, 'error_msg': error_msg,
                'all_titles': titles, 'cleaned_dates': dates,
                'collected_data': kw_data, 'all_unfiltered': all_data,
                'url_search': pending_url,
            },
        }

    return _build_result(site_name, row, titles, dates, kw_data, all_data,
                         failed, auto_fixed, False, error_msg)


# ─────────────────────────────────────────────
# 병렬 크롤링 (Phase 1 + Phase 2)
# ─────────────────────────────────────────────
def run_crawling(df: pd.DataFrame, gc,
                 static_workers: int = 15, dynamic_workers: int = 4) -> tuple:
    kw_data, logs, all_data = [], [], []
    pending_claude = []

    def collect(futures, desc: str):
        for fut in tqdm(as_completed(futures), total=len(futures), desc=desc):
            try:
                r = fut.result()
                if 'pending' in r:
                    pending_claude.append(r['pending'])
                else:
                    kw_data.extend(r['data'])
                    all_data.extend(r.get('all_data', []))
                    logs.append(r['log'])
            except Exception as e:
                logger.error(f"[병렬 오류] {e}")

    df_http     = df[df['fetch_type'] == 'http']      # HTTP 기반 (정적 + POST)
    df_selenium = df[df['fetch_type'] == 'selenium']  # Selenium 기반

    # Phase 1: 병렬 크롤링 (gc 불필요)
    with ThreadPoolExecutor(max_workers=static_workers) as ex:
        collect({ex.submit(crawl_site, row): i for i, row in df_http.iterrows()},
                f"HTTP ({len(df_http)}개)")
    with ThreadPoolExecutor(max_workers=dynamic_workers) as ex:
        collect({ex.submit(crawl_site, row): i for i, row in df_selenium.iterrows()},
                f"Selenium ({len(df_selenium)}개)")

    # Phase 2: Claude 순차 처리
    if pending_claude:
        logger.info(f"[Phase 2] Claude 처리: {len(pending_claude)}개")
        for p in tqdm(pending_claude, desc=f"Claude ({len(pending_claude)}개)"):
            try:
                r = process_with_claude(p, gc)
                kw_data.extend(r['data']); all_data.extend(r.get('all_data', []))
                logs.append(r['log'])
            except Exception as e:
                site = p.get('site_name', '?')
                logger.error(f"[Claude 처리 오류] {site}: {e}")
                logs.append({
                    'SITE_NAME': site, 'URL': p.get('row', {}).get('URL', ''),
                    'len_tbody': 0, 'unique_date': 0, 'min_date': '', 'max_date': '',
                    'status': '실패(Claude오류)', 'error_msg': str(e)[:80],
                    'auto_fixed': '', '최신제목': '', '최신날짜': '',
                })

    return pd.DataFrame(kw_data), pd.DataFrame(logs), pd.DataFrame(all_data)


# ─────────────────────────────────────────────
# Google Sheets 업로드
# ─────────────────────────────────────────────
def upload_to_sheet(gc, df: pd.DataFrame, sheet_name: str, keyword_tab: bool = False):
    if gc is None or df.empty:
        return
    try:
        sh = gc.open_by_key(GOOGLE_SHEET_ID)
        try:
            ws = sh.worksheet(sheet_name)
        except gspread.exceptions.WorksheetNotFound:
            ws = sh.add_worksheet(title=sheet_name, rows="5000", cols="30")

        existing = pd.DataFrame(ws.get_all_records())

        # 기존 확인여부/비고 보존
        meta = {}
        if not existing.empty:
            for _, row in existing.iterrows():
                key = (str(row.get('출처', '')), str(row.get('제목', '')))
                meta[key] = {'확인여부': row.get('확인여부', ''), '비고': row.get('비고', '')}

        combined = pd.concat([existing, df], ignore_index=True) if not existing.empty else df.copy()
        if '출처' in combined.columns and '제목' in combined.columns:
            combined = combined.drop_duplicates(subset=['출처', '제목'], keep='last')

        # TTL: RESULT_TTL_DAYS 이상 된 데이터 자동 정리
        if '작성일' in combined.columns:
            cutoff_ttl = (now_kst() - timedelta(days=RESULT_TTL_DAYS)).strftime('%Y-%m-%d')
            before = len(combined)
            combined = combined[combined['작성일'].astype(str) >= cutoff_ttl]
            removed = before - len(combined)
            if removed > 0:
                logger.info(f"[TTL 정리] '{sheet_name}' {removed}행 제거 (기준: {cutoff_ttl})")

        if keyword_tab:
            if '지역' not in combined.columns:
                combined.insert(2, '지역', combined['출처'].apply(extract_region))
            today_dt = now_kst().date()
            combined['경과일'] = combined['작성일'].apply(
                lambda d: (today_dt - pd.to_datetime(d).date()).days
                if d else '')
            for kw in FILTER_KEYWORDS:
                combined[kw] = combined.get('키워드', pd.Series(dtype=str)).apply(
                    lambda v: 'Y' if kw in str(v) else '')
            for col in ('확인여부', '비고'):
                if col not in combined.columns:
                    combined[col] = ''
            for idx, row in combined.iterrows():
                key = (str(row.get('출처', '')), str(row.get('제목', '')))
                m = meta.get(key, {})
                for col in ('확인여부', '비고'):
                    if m.get(col):
                        combined.at[idx, col] = m[col]
            combined['작성일'] = pd.to_datetime(combined['작성일'], errors='coerce')
            combined = combined.sort_values('작성일', ascending=False)
            combined['작성일'] = combined['작성일'].dt.strftime('%Y-%m-%d').fillna('')
            if 'URL' in combined.columns:
                combined['원문링크'] = combined.apply(
                    lambda r: f'=HYPERLINK("{r["URL"]}","{r["제목"][:30].replace(chr(34), "")}")'
                    if str(r.get('URL', '')).startswith('http') else '', axis=1)

        combined = combined.fillna("").astype(str)
        ws.clear()
        ws.update([combined.columns.tolist()] + combined.values.tolist(),
                  value_input_option='USER_ENTERED')
        logger.info(f"[업로드 완료] '{sheet_name}' {len(combined)}행")
    except Exception as e:
        logger.error(f"[업로드 오류] {sheet_name}: {e}")


def upload_log(gc, df: pd.DataFrame, crawled_time: str = ""):
    if gc is None or df.empty:
        return
    try:
        sh = gc.open_by_key(GOOGLE_SHEET_ID)
        try:
            ws = sh.worksheet("크롤링로그")
        except gspread.exceptions.WorksheetNotFound:
            ws = sh.add_worksheet(title="크롤링로그", rows="5000", cols="20")

        existing = pd.DataFrame()
        try:
            existing = pd.DataFrame(ws.get_all_records())
        except Exception:
            pass

        # 이전 연속실패횟수 로드
        prev: dict[str, int] = {}
        if not existing.empty and '연속실패횟수' in existing.columns:
            for _, row in existing.iterrows():
                prev[str(row.get('SITE_NAME', ''))] = int(row.get('연속실패횟수', 0) or 0)

        df = df.copy()
        df['수집일시'] = crawled_time
        df = df.drop_duplicates(subset=['SITE_NAME'], keep='last')

        def calc_fail_count(row):
            p = prev.get(str(row.get('SITE_NAME', '')), 0)
            s = str(row.get('status', ''))
            if s.startswith('실패'): return p + 1
            if '스킵' in s:         return p
            return 0

        df['연속실패횟수'] = df.apply(calc_fail_count, axis=1)

        # 기존 미실행 사이트 보존 + 이번 결과 병합
        if not existing.empty:
            new_sites = set(df['SITE_NAME'].astype(str))
            merged = pd.concat(
                [existing[~existing['SITE_NAME'].astype(str).isin(new_sites)], df],
                ignore_index=True)
        else:
            merged = df

        merged = merged.fillna("").astype(str)
        ws.clear()
        ws.update([merged.columns.tolist()] + merged.values.tolist())
        logger.info(f"[로그 업로드] 전체 {len(merged)}행 (갱신 {len(df)}행)")
    except Exception as e:
        logger.error(f"[로그 업로드 오류] {e}")


def upload_dashboard(gc, df_log: pd.DataFrame, df_kw: pd.DataFrame, crawled_time: str):
    if gc is None:
        return
    try:
        sh = gc.open_by_key(GOOGLE_SHEET_ID)
        try:
            ws = sh.worksheet("📊대시보드")
        except Exception:
            ws = sh.add_worksheet(title="📊대시보드", rows="50", cols="10")

        total   = len(df_log)
        success = len(df_log[df_log['status'] == '성공'])
        healed  = len(df_log[df_log['status'] == '자가치유성공'])
        zero_s  = len(df_log[df_log['status'] == 'Zero-Selector성공'])
        skipped = len(df_log[df_log['status'].str.contains('스킵', na=False)])
        failed  = len(df_log[df_log['status'].str.startswith('실패', na=False)])
        rate    = f"{round((success + healed + zero_s) / max(total - skipped, 1) * 100, 1)}%"

        kw_counts = {kw: int(df_kw['키워드'].str.contains(kw, na=False).sum())
                     for kw in FILTER_KEYWORDS} if not df_kw.empty and '키워드' in df_kw.columns else {}
        region_counts = {}
        if not df_kw.empty and '출처' in df_kw.columns:
            region_counts = {k: int(v) for k, v in
                             df_kw['출처'].apply(extract_region).value_counts().items()}

        rows_data = [
            ["📊 공고 수집 대시보드", "", f"기준: {crawled_time}"], [""],
            ["▶ 크롤링 현황"], ["구분", "건수"],
            ["전체 대상", total], ["✅ 성공", success],
            ["🤖 Zero-Selector 성공", zero_s], ["🔧 자가치유 성공", healed],
            ["❌ 실패", failed], ["성공률", rate], [""],
            ["▶ 오늘 키워드 공고", len(df_kw), "건"], [""],
            ["▶ 키워드별 집계"], ["키워드", "건수"],
        ]
        rows_data += [[kw, cnt] for kw, cnt in kw_counts.items()]
        rows_data += [[""], ["▶ 지역별 집계"], ["지역", "건수"]]
        rows_data += [[r, c] for r, c in sorted(region_counts.items(), key=lambda x: -x[1])]

        ws.clear()
        ws.update(rows_data, value_input_option='USER_ENTERED')
        logger.info("[대시보드 업로드 완료]")
    except Exception as e:
        logger.error(f"[대시보드 업로드 오류] {e}")


# ─────────────────────────────────────────────
# 이메일
# ─────────────────────────────────────────────
def _send_email(subject: str, body: str):
    if not all([EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_RECEIVER]):
        return
    try:
        msg = MIMEText(body, 'plain', 'utf-8')
        msg['Subject'] = subject
        msg['From']    = EMAIL_SENDER
        msg['To']      = EMAIL_RECEIVER
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
            smtp.login(EMAIL_SENDER, EMAIL_PASSWORD)
            smtp.send_message(msg)
    except Exception as e:
        logger.error(f"[이메일 오류] {e}")

def send_keyword_email(df: pd.DataFrame, crawled_time: str):
    if df.empty:
        return
    lines = [f"[신규 키워드 공고] {crawled_time}\n총 {len(df)}건:\n"]
    for _, r in df.iterrows():
        lines.append(f"  [{r.get('출처','')}] {r.get('제목','')}")
        lines.append(f"    {r.get('작성일','')} | {r.get('키워드','')} | {r.get('URL','')}\n")
    _send_email(f"[공고알림] 신규 {len(df)}건 ({now_kst():%m/%d %H:%M} KST)", "\n".join(lines))

def send_failure_email(df_log: pd.DataFrame):
    failed = df_log[df_log['status'].str.startswith('실패', na=False)]
    if failed.empty:
        return
    lines = [f"[크롤링 실패 알림] {now_kst():%Y-%m-%d} - {len(failed)}개 수동 확인 필요\n"]
    for _, r in failed.iterrows():
        lines.append(f"  - {r['SITE_NAME']}: {r['error_msg']}")
    _send_email(f"[크롤러] {len(failed)}개 실패 ({now_kst():%m/%d} KST)", "\n".join(lines))


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--test', type=int, metavar='N', default=0,
                        help='테스트 모드: 정적 N + 동적 N개 실행')
    args = parser.parse_args()

    today        = now_kst()
    crawled_time = today.strftime('%Y-%m-%d %H:%M:%S KST')
    today_str    = today.strftime('%Y%m%d')
    cutoff       = (today - timedelta(days=DAYS_RANGE)).date()

    logger.info(f"===== 크롤링 시작 v5: {crawled_time} =====")

    gc = get_gc()

    # 클릭설정 로드 (GSheets/Excel 클릭설정 시트 → 코드 기본값 순)
    global _click_config
    _click_config = load_click_config(gc)

    df = load_sites(gc)
    logger.info(f"총 {len(df)}개 사이트")

    if args.test:
        n  = args.test
        df = pd.concat([df[df['fetch_type'] == 'http'].head(n),
                        df[df['fetch_type'] == 'selenium'].head(n)], ignore_index=True)
        logger.info(f"[테스트] {len(df)}개 실행")

    df_kw, df_log, df_all = run_crawling(df, gc)

    # 날짜 필터링 + 중복 제거
    def filter_by_date(d: pd.DataFrame) -> pd.DataFrame:
        if d.empty:
            return d
        d = d.copy()
        d['작성일'] = pd.to_datetime(d['작성일'], format='%Y-%m-%d', errors='coerce')
        d = d[d['작성일'].dt.date >= cutoff]
        d = d.drop_duplicates(subset=['출처', '제목'], keep='last')
        d['수집일'] = crawled_time
        return d

    df_kw_f  = filter_by_date(df_kw)
    df_all_f = filter_by_date(df_all)

    if not df_log.empty:
        s = len(df_log[df_log['status'] == '성공'])
        h = len(df_log[df_log['status'] == '자가치유성공'])
        z = len(df_log[df_log['status'] == 'Zero-Selector성공'])
        f = len(df_log[df_log['status'].str.startswith('실패', na=False)])
        logger.info(f"===== 결과: 성공 {s} | Zero {z} | 자가치유 {h} | 실패 {f} / 전체 {len(df_log)} =====")

    # 로컬 Excel 저장 (3 sheets)
    excel_path = f'./result_{today_str}.xlsx'
    with pd.ExcelWriter(excel_path, engine='openpyxl') as writer:
        df_log.to_excel(writer, sheet_name='크롤링로그', index=False)
        if not df_kw_f.empty:
            df_kw_f.to_excel(writer, sheet_name='키워드공고', index=False)
        if not df_all_f.empty:
            df_all_f.to_excel(writer, sheet_name='전체공고', index=False)
    logger.info(f"[로컬 저장] {excel_path}")

    # Google Sheets 업로드
    upload_to_sheet(gc, df_kw_f,  "✅키워드공고", keyword_tab=True)
    upload_to_sheet(gc, df_all_f, "📋전체공고(키워드제외)")
    upload_log(gc, df_log, crawled_time)
    upload_dashboard(gc, df_log, df_kw_f, crawled_time)

    # 이메일 알림
    send_keyword_email(df_kw_f, crawled_time)
    send_failure_email(df_log)

    logger.info(f"===== 완료: {now_kst():%Y-%m-%d %H:%M:%S} KST =====")


if __name__ == "__main__":
    main()
