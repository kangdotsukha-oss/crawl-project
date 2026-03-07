"""
공공기관 고시/공고 자동 크롤러 v5 - Hybrid (Selector-first + Claude fallback)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
전략:
  1순위: CSS 셀렉터로 파싱 (v4 방식, Claude 0호출, 무료)
  2순위: 셀렉터 실패 시 trafilatura + Claude Haiku 추출 (v5 방식)
  3순위: Claude 크레딧 부족 시 regex fallback 자동 전환

v4 대비 개선:
  - Selenium → Playwright async (속도↑, 안정성↑)
  - "알 수 없는 타입" 버그 수정 (page 2 crawl_type 공백 처리)
  - git push race condition 해결 (concurrency 그룹, workflow에 적용)
  - asyncio 병렬화 (정적 10개 / 동적 4개 동시)
  - SQLite 로컬 저장 추가

복구: git checkout main  또는  git checkout v4-stable
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import asyncio
import json
import logging
import math
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import anthropic
import gspread
import requests
import trafilatura
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from requests.packages.urllib3.exceptions import InsecureRequestWarning

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

# ─────────────────────────────────────────
# 환경변수
# ─────────────────────────────────────────
ANTHROPIC_API_KEY       = os.environ.get("ANTHROPIC_API_KEY", "")
GOOGLE_SHEET_ID         = os.environ.get("GOOGLE_SHEET_ID", "")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")

# ─────────────────────────────────────────
# 상수
# ─────────────────────────────────────────
KST          = timezone(timedelta(hours=9))
DAYS_RANGE   = 1
MAX_PAGES    = 5
STATIC_CONC  = 10
DYNAMIC_CONC = 8
PAGE_TIMEOUT = 30_000
FILTER_KEYWORDS = ['특허', '제안', '심의', '공법', '실시설계', '보수보강']
SITES_FILE   = Path(__file__).parent / "sites.json"
DB_FILE      = Path(__file__).parent / "crawl_v5.db"

# ─────────────────────────────────────────
# 로깅
# ─────────────────────────────────────────
class _KSTFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=KST)
        return dt.strftime('%Y-%m-%d %H:%M:%S')

_fmt = _KSTFormatter("%(asctime)s [%(levelname)s] %(message)s")
_fh  = logging.FileHandler("crawl_v5.log", encoding="utf-8")
_fh.setFormatter(_fmt)
_sh  = logging.StreamHandler()
_sh.setFormatter(_fmt)
logging.basicConfig(level=logging.INFO, handlers=[_fh, _sh])
logger = logging.getLogger(__name__)

def now_kst():
    return datetime.now(KST)

# ─────────────────────────────────────────
# 유틸
# ─────────────────────────────────────────
def is_empty(v) -> bool:
    if v is None or v == "": return True
    if isinstance(v, float) and math.isnan(v): return True
    return False

REGION_MAP = [
    ('서울','서울'),('부산','부산'),('대구','대구'),('인천','인천'),
    ('광주','광주'),('대전','대전'),('울산','울산'),('세종','세종'),
    ('경기','경기도'),('강원','강원도'),
    ('충북','충청도'),('충남','충청도'),('충청','충청도'),
    ('전북','전라도'),('전남','전라도'),('전라','전라도'),
    ('경북','경상도'),('경남','경상도'),('경상','경상도'),
    ('제주','제주도'),
]

def extract_region(name: str) -> str:
    for key, region in REGION_MAP:
        if key in name: return region
    return '기타'

# ─────────────────────────────────────────
# SQLite
# ─────────────────────────────────────────
def init_db():
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS announcements (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            site_id      TEXT,
            site_name    TEXT,
            title        TEXT,
            date         TEXT,
            url          TEXT,
            keyword      TEXT,
            collected_at TEXT,
            UNIQUE(site_name, title, date)
        );
        CREATE TABLE IF NOT EXISTS crawl_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at     TEXT,
            site_id    TEXT,
            site_name  TEXT,
            status     TEXT,
            item_count INTEGER,
            error_msg  TEXT,
            method     TEXT
        );
    """)
    con.commit()
    return con

# ─────────────────────────────────────────
# 날짜 처리
# ─────────────────────────────────────────
def fix_date_format(date_str: str) -> str:
    if not date_str or not isinstance(date_str, str):
        return ""
    date_str = date_str.strip()
    date_str = date_str.split('~')[0].strip()
    date_str = re.sub(r'[./]', '-', date_str)
    date_str = re.sub(r'\s+', '', date_str)

    # YYYYMMDD → YYYY-MM-DD
    if re.fullmatch(r'\d{8}', date_str):
        date_str = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
    # 2자리 연도
    if re.match(r'^\d{2}-', date_str):
        date_str = '20' + date_str
    # 앞 4자리만 추출 (길이 초과 케이스)
    m = re.search(r'\d{4}-\d{2}-\d{2}', date_str)
    if m:
        date_str = m.group()

    return date_str[:10] if len(date_str) >= 10 else date_str

def is_recent(date_str: str, days: int = DAYS_RANGE) -> bool:
    if not date_str:
        return True
    try:
        dt = datetime.strptime(date_str[:10], '%Y-%m-%d')
        cutoff = now_kst().replace(tzinfo=None) - timedelta(days=days)
        return dt >= cutoff
    except Exception:
        return True

def matches_keyword(title: str) -> tuple[bool, str]:
    for kw in FILTER_KEYWORDS:
        if kw in title:
            return True, kw
    return False, ""

# ─────────────────────────────────────────
# Playwright 클릭 크롤링 설정 (v4 CLICK_CRAWL_CONFIG 통합)
# ─────────────────────────────────────────
CLICK_CRAWL_CONFIG = {
    'cd':  {'type':'css',   'tpl':"body > form > div.default_board > div.paging > table > tbody > tr > td:nth-child(4) > span:nth-child({n}) > a",      'n_calc': lambda p: (p-1)*2+1},
    'cd1': {'type':'css',   'tpl':"#form1 > div.pgeAbs.mt30 > p > span:nth-child({n}) > a",                                                             'n_calc': lambda p: p},
    'cd2': {'type':'xpath', 'tpl':"/html/body/div[2]/div[2]/div/section[2]/div[1]/form/div[2]/a[{n}]",                                                  'n_calc': lambda p: p+2},
    'cd3': {'type':'css',   'tpl':"#txt > div.text-center > div > ul > li:nth-child({n}) > a",                                                          'n_calc': lambda p: p+2},
    'cd4': {'type':'css',   'tpl':"#dataForm > div.pagination.mt-md-4 > a:nth-child({n})",                                                              'n_calc': lambda p: p},
    'cd5': {'type':'css',   'tpl':"#cont-body > div.paging > div > div > a:nth-child({n})",                                                             'n_calc': lambda p: p+2},
    'cd6': {'type':'css',   'tpl':"#contentDiv > form > table.MAT10 > tbody > tr > td > table > tbody > tr > td > table > tbody > tr > td:nth-child(4) > span:nth-child({n}) > a", 'n_calc': lambda p: (p-1)*2+1},
    'cd7': {'type':'css',   'tpl':"#list > div.bod_page > a:nth-child({n})",                                                                            'n_calc': lambda p: p+2},
    'cd8': {'type':'css',   'tpl':"#board > div:nth-child(4) > table > tbody > tr > td > table > tbody > tr > td:nth-child(4) > span:nth-child({n}) > a",'n_calc': lambda p: (p-1)*2+1},
    'cd9': {'type':'css',   'tpl':"body > form > div.sb_w > div:nth-child(3) > table > tbody > tr > td > table > tbody > tr > td > table > tbody > tr > td:nth-child(4) > span:nth-child({n}) > a", 'n_calc': lambda p: (p-1)*2+1},
    'cd10':{'type':'xpath', 'tpl':"/html/body/form/table[3]/tbody/tr/td/table[2]/tbody/tr/td/table/tbody/tr/td/table/tbody/tr/td[4]/span[{n}]/a",       'n_calc': lambda p: (p-1)*2+1},
    'cd11':{'type':'xpath', 'tpl':"//*[@id='list']/div[2]/div/a[{n}]",                                                                                  'n_calc': lambda p: p+2},
    'cd12':{'type':'css',   'tpl':"#sidoGosiAPIVO > div.pagination > div.normal_pagination > a:nth-child({n})",                                          'n_calc': lambda p: p+2},
    'cd13':{'type':'css',   'tpl':"#txt > div > div.text-center > ul > li:nth-child({n}) > a",                                                          'n_calc': lambda p: p+2},
    'cd14':{'type':'css',   'tpl':"body > form > div > div > div.p-pagination > div > span.p-page__link-group > a:nth-child({n})",                       'n_calc': lambda p: p},
    'cd15':{'type':'css',   'tpl':"body > form > div > div.paging > table > tbody > tr > td:nth-child(4) > span:nth-child({n}) > a",                    'n_calc': lambda p: (p-1)*2+1},
    'cd16':{'type':'css',   'tpl':"body > form > table > tbody > tr:nth-child(2) > td:nth-child(2) > table > tbody > tr:nth-child(7) > td > table > tbody > tr > td:nth-child({n}) > a", 'n_calc': lambda p: p+4},
    'cd17':{'type':'css',   'tpl':"body > form > div.board > div > div > table > tbody > tr > td:nth-child(2) > table > tbody > tr > td:nth-child(4) > span:nth-child({n})", 'n_calc': lambda p: (p-1)*2+1},
    'cd18':{'type':'css',   'tpl':"#contents > div > div.p-wrap.bbs.bbs_list > div.p-pagination > div.p-page_link-group > a:nth-child({n})",             'n_calc': lambda p: p},
    'cd19':{'type':'css',   'tpl':"#contents > form > table:nth-child(23) > tbody > tr:nth-child(1) > td > table > tbody > tr > td > table > tbody > tr > td:nth-child(4) > span:nth-child({n}) > a", 'n_calc': lambda p: (p-1)*2+1},
    'cd20':{'type':'css',   'tpl':"body > form > div.pagination > a:nth-child({n})",                                                                    'n_calc': lambda p: p},
    'cd21':{'type':'css',   'tpl':"body > div.pagination > table > tbody > tr > td:nth-child(4) > span:nth-child({n}) > a",                             'n_calc': lambda p: (p-1)*2+1},
    'cd22':{'type':'css',   'tpl':"body > div.pagination > a:nth-child({n})",                                                                           'n_calc': lambda p: p},
    'cd23':{'type':'xpath', 'tpl':"/html/body/div[4]/section/div/div/div[2]/div/div/div[3]/div/ul/ul/li[{n}]",                                          'n_calc': lambda p: p+2},
    'cd24':{'type':'css',   'tpl':"#content_area > div.container > div > div.content > div.board_list > div.paging > ul > li:nth-child({n}) > a",       'n_calc': lambda p: p},
    'cd25':{'type':'css',   'tpl':"#eminwonWrap > div.pagination > ul > li:nth-child({n}) > a",                                                         'n_calc': lambda p: p},
    'cd26':{'type':'css',   'tpl':"#listForm > div.box_page > a:nth-child({n})",                                                                        'n_calc': lambda p: p+2},
    'cd27':{'type':'xpath', 'tpl':"/html/body/form/table[2]/tbody/tr/td/table[2]/tbody/tr/td/table/tbody/tr/td/table/tbody/tr/td[4]/span[{n}]/a",       'n_calc': lambda p: (p-1)*2+1},
    'cd28':{'type':'css',   'tpl':"#A-Contents > div.pager > table > tbody > tr > td > table > tbody > tr > td:nth-child(4) > span:nth-child({n}) > a", 'n_calc': lambda p: (p-1)*2+1},
    'cd29':{'type':'css',   'tpl':"#contentsArea > div.pager > a:nth-child({n})",                                                                       'n_calc': lambda p: p+3},
    'cd30':{'type':'xpath', 'tpl':"/html/body/form/div/table/tbody/tr/td/table/tbody/tr/td/table/tbody/tr/td[4]/span[{n}]",                             'n_calc': lambda p: (p-1)*2+1},
    'cd31':{'type':'css',   'tpl':"body > form > section > div.pager > a:nth-child({n})",                                                               'n_calc': lambda p: p+2},
    'cd32':{'type':'xpath', 'tpl':"/html/body/div/main/div/div/div[2]/div[2]/div[3]/a[{n}]",                                                            'n_calc': lambda p: p+2},
    'cd33':{'type':'css',   'tpl':"body > form > table:nth-child(12) > tbody > tr > td > table:nth-child(3) > tbody > tr > td > table > tbody > tr > td > table > tbody > tr > td:nth-child(4) > span[{n}]/a", 'n_calc': lambda p: (p-1)*2+1},
    'cd34':{'type':'css',   'tpl':"#paging-tag > ul > li:nth-child({n})",                                                                               'n_calc': lambda p: p+2},
}

# ─────────────────────────────────────────
# User-Agent
# ─────────────────────────────────────────
UA_LIST = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

FIREWALL_KEYWORDS = [
    '웹방화벽','Web Firewall','WAPPLES','Access Denied','접근이 차단',
    '차단되었습니다','Blocked','Forbidden','보안 위반','CloudFlare',
]

def is_firewall(html: str, status_code: int = 200) -> bool:
    if status_code in (403, 406):
        return True
    return any(kw in html for kw in FIREWALL_KEYWORDS)

# ─────────────────────────────────────────
# 정적 수집 (requests)
# ─────────────────────────────────────────
def fetch_static(url: str, ua_idx: int = 0) -> Optional[str]:
    headers = {
        "User-Agent": UA_LIST[ua_idx % len(UA_LIST)],
        "Accept-Language": "ko-KR,ko;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    for enc in ['utf-8', 'euc-kr', 'cp949']:
        try:
            r = requests.get(url, headers=headers, timeout=(8, 20), verify=False)
            r.raise_for_status()
            r.encoding = enc
            if is_firewall(r.text, r.status_code):
                # UA 로테이션 재시도
                for i in range(1, len(UA_LIST)):
                    h2 = {**headers, "User-Agent": UA_LIST[i]}
                    r2 = requests.get(url, headers=h2, timeout=(8, 20), verify=False)
                    if not is_firewall(r2.text, r2.status_code):
                        r2.encoding = enc
                        return r2.text
                return None
            return r.text
        except Exception:
            pass
    return None

# ─────────────────────────────────────────
# 동적 수집 (Playwright)
# ─────────────────────────────────────────
async def fetch_dynamic(url: str, page, wait_sec: int = 5) -> Optional[str]:
    try:
        await page.goto(url, wait_until='domcontentloaded', timeout=PAGE_TIMEOUT)
        try:
            await page.wait_for_load_state('networkidle', timeout=8000)
        except Exception:
            pass
        await asyncio.sleep(wait_sec)
        return await page.content()
    except PlaywrightTimeout:
        try:
            return await page.content()
        except Exception:
            return None
    except Exception as e:
        logger.debug(f"[동적 실패] {url}: {e}")
        return None

async def fetch_click_page(url: str, page, ct: str, page_num: int) -> Optional[str]:
    """cd1~cd34 클릭 기반 페이지네이션 (Playwright)"""
    config = CLICK_CRAWL_CONFIG.get(ct)
    if not config:
        return None
    try:
        n = config['n_calc'](page_num)
        selector = config['tpl'].replace('{n}', str(n))
        await page.goto(url, wait_until='domcontentloaded', timeout=PAGE_TIMEOUT)
        try:
            await page.wait_for_load_state('networkidle', timeout=5000)
        except Exception:
            pass
        await asyncio.sleep(3)
        if config['type'] == 'xpath':
            el = page.locator(f'xpath={selector}')
        else:
            el = page.locator(selector)
        await el.click(timeout=5000)
        await asyncio.sleep(3)
        return await page.content()
    except Exception as e:
        logger.debug(f"[클릭 실패] {ct} p{page_num}: {e}")
        return None

# ─────────────────────────────────────────
# URL 페이지네이션
# ─────────────────────────────────────────
_PAGE_PARAMS = ['pageIndex','page','Page','pageNo','cpn','pageid','p','Page2']

def update_url_for_page(url: str, page_num: int, div: str) -> Optional[str]:
    if page_num == 1:
        return url
    if div != 'V2':
        return None  # URL 기반 페이지네이션 없음

    for param in _PAGE_PARAMS:
        pattern = rf'({re.escape(param)}=)\d+'
        if re.search(pattern, url):
            return re.sub(pattern, rf'\g<1>{page_num}', url)
    if 'offset=' in url:
        return re.sub(r'offset=\d+', f'offset={(page_num-1)*15}', url)
    if 'Start=' in url:
        return re.sub(r'Start=\d+', f'Start={(page_num-1)*10}', url)
    return url  # V2인데 파라미터 없으면 원본 유지

def resolve_crawl_type(ct: str, ct2: str, page_num: int) -> str:
    """
    v4의 update_crawl_type 버그 수정:
    page 2+에서 ct2가 빈 문자열('')이면 ct2가 아닌 원래 ct 사용
    """
    if page_num == 1:
        return ct
    if ct2 and ct2.strip():
        return ct2.strip()
    return ct

# ─────────────────────────────────────────
# 셀렉터 기반 파싱 (1순위)
# ─────────────────────────────────────────
def parse_with_selector(html: str, site: dict) -> list[dict]:
    """CSS 셀렉터로 공고 목록 파싱 (v4 방식)"""
    sel = site.get('selector', {})
    tb_sel   = sel.get('table_body', '')
    ti_sel   = sel.get('title', '')
    dt_sel   = sel.get('date', '')

    if not tb_sel or not ti_sel or not dt_sel:
        return []

    try:
        soup = BeautifulSoup(html, 'html.parser')

        # 대전광역시 특수 처리 (table_body 2번째 요소)
        if site.get('name') == '대전광역시고시공고':
            candidates = soup.select(tb_sel)
            table_body = candidates[1] if len(candidates) > 1 else (candidates[0] if candidates else None)
        else:
            table_body = soup.select_one(tb_sel)

        if not table_body:
            return []

        titles = table_body.select(ti_sel)
        dates  = table_body.select(dt_sel)

        # 충청도_서천군 특수처리: '등록일' 포함 td 제외
        if site.get('name') == '충청도_서천군':
            dates = [d for d in dates if '등록일' not in d.get_text(strip=True)]

        items = []
        for title_el, date_el in zip(titles, dates):
            raw_title = title_el.get_text(strip=True).replace('\r','').replace('\n','').replace('\t','').strip()
            raw_date  = date_el.get_text(separator=' ', strip=True)

            # 날짜 파싱
            if '공고부서 :' in raw_date:
                raw_date = raw_date.split('공고부서 :')[-2].split('등록일 :')[-1].strip()
            elif '게재일 :' in raw_date:
                raw_date = raw_date.split('게재일 :')[1].strip()
            else:
                raw_date = raw_date.replace('.', '-').replace('/', '-').replace('등록일 :', '').strip()

            date_str = fix_date_format(raw_date)
            if raw_title:
                items.append({'title': raw_title, 'date': date_str})

        return items
    except Exception as e:
        logger.debug(f"[셀렉터 파싱 오류] {site.get('name')}: {e}")
        return []

# ─────────────────────────────────────────
# Claude fallback (2순위)
# ─────────────────────────────────────────
_claude_available: bool = True
_claude_client: Optional[anthropic.Anthropic] = None

def get_claude():
    global _claude_client
    if _claude_client is None:
        _claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _claude_client

def extract_with_claude(site_name: str, text: str) -> list[dict]:
    global _claude_available

    if not text or len(text.strip()) < 50:
        return []

    if ANTHROPIC_API_KEY and _claude_available:
        result = _call_claude(site_name, text)
        if result is not None:
            return result
        logger.warning("[Claude→regex fallback 전환]")
        _claude_available = False

    return _extract_regex(text)

def _call_claude(site_name: str, text: str) -> Optional[list[dict]]:
    prompt = f"""다음은 한국 공공기관 고시/공고 목록 페이지의 텍스트입니다.
사이트명: {site_name}

텍스트:
{text[:6000]}

위 텍스트에서 고시/공고 항목들을 추출하세요.
- 각 항목의 제목(title)과 날짜(date)를 추출
- 날짜는 YYYY-MM-DD 형식으로 정규화
- 날짜 불명 시 "" 표시
- 공고 없으면 빈 배열
- JSON 배열만 출력 (다른 설명 없이)

[{{"title": "공고 제목", "date": "YYYY-MM-DD"}}, ...]"""
    try:
        resp = get_claude().messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = resp.content[0].text.strip()
        m = re.search(r'\[.*\]', raw, re.DOTALL)
        if m:
            items = json.loads(m.group())
            return [i for i in items if isinstance(i, dict) and i.get('title')]
        return []
    except anthropic.BadRequestError as e:
        if 'credit' in str(e).lower() or 'balance' in str(e).lower():
            return None  # 크레딧 부족 → fallback
        logger.warning(f"[Claude 오류] {site_name}: {e}")
        return []
    except Exception as e:
        logger.warning(f"[Claude 오류] {site_name}: {e}")
        return []

# ─────────────────────────────────────────
# Regex fallback (3순위)
# ─────────────────────────────────────────
_DATE_RE   = re.compile(r'(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})')
_KO_RE     = re.compile(r'[\uAC00-\uD7A3]{2,}')

def _extract_regex(text: str) -> list[dict]:
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    items, seen = [], set()

    for i, line in enumerate(lines):
        m = _DATE_RE.search(line)
        if not m:
            continue
        try:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if not (2020 <= y <= 2030 and 1 <= mo <= 12 and 1 <= d <= 31):
                continue
            date_str = f"{y:04d}-{mo:02d}-{d:02d}"
        except Exception:
            continue

        title = ""
        for offset in [0, -1, -2, 1]:
            idx = i + offset
            if 0 <= idx < len(lines):
                cand = lines[idx]
                if _KO_RE.search(cand) and not re.fullmatch(r'[\d\s.\-/|]+', cand):
                    clean = re.sub(r'\|\s*\d+\s*\|?', '', cand)
                    clean = re.sub(r'^\s*\d+\s+', '', clean)
                    clean = re.sub(r'\s{2,}', ' ', clean).strip('| \t')
                    if len(clean) >= 5:
                        title = clean[:100]
                        break

        if title and title not in seen:
            seen.add(title)
            items.append({'title': title, 'date': date_str})

    return items[:30]

def preprocess_html(html: str) -> str:
    if not html:
        return ""
    text = trafilatura.extract(html, include_tables=True, favor_recall=True)
    if not text or len(text) < 100:
        try:
            soup = BeautifulSoup(html, 'html.parser')
            for tag in soup(['script','style','nav','footer','header','aside']):
                tag.decompose()
            for td in soup.find_all('td'): td.insert_after('\t')
            for tr in soup.find_all('tr'): tr.insert_after('\n')
            fallback = soup.get_text(separator='\n', strip=True)
            fallback = re.sub(r'\n{3,}', '\n\n', fallback)
            if len(fallback) > len(text or ''):
                text = fallback
        except Exception:
            pass
    return (text or '')[:8000]

# ─────────────────────────────────────────
# 단일 사이트 크롤링
# ─────────────────────────────────────────
async def crawl_site(site: dict, browser=None) -> dict:
    site_id   = site['id']
    site_name = site['name']
    url       = site['url']
    dynamic   = site['dynamic']
    ct        = site.get('crawl_type', 's') or 's'
    ct2       = site.get('ct2', '') or ''
    div       = site.get('div', '') or ''

    collected, all_items = [], []
    all_dates = []
    error_msg = ""
    status    = "성공"
    method    = "selector"  # 어떤 방식으로 추출했는지 기록
    page      = None

    if site.get('skip'):
        return _skip_result(site)

    logger.info(f"[시작] {site_name}")

    try:
        needs_click = ct in CLICK_CRAWL_CONFIG or ct2 in CLICK_CRAWL_CONFIG
        if (dynamic or needs_click) and browser:
            context = await browser.new_context(user_agent=UA_LIST[0], locale='ko-KR')
            page = await context.new_page()

        for page_num in range(1, MAX_PAGES + 1):
            current_ct = resolve_crawl_type(ct, ct2, page_num)

            # ── HTML 수집 ──
            html = None
            if page_num == 1:
                page_url = url
            else:
                page_url = update_url_for_page(url, page_num, div)
                if page_url is None and current_ct not in CLICK_CRAWL_CONFIG:
                    break  # 더 이상 페이지 없음

            if current_ct == 's':
                html = fetch_static(page_url or url)
                if not html:
                    for i in range(1, len(UA_LIST)):
                        html = fetch_static(page_url or url, i)
                        if html: break
            elif current_ct in ('d', 'd1', 'd2') and page:
                html = await fetch_dynamic(page_url or url, page)
            elif current_ct in CLICK_CRAWL_CONFIG and page:
                if page_num == 1:
                    html = await fetch_dynamic(url, page)
                else:
                    html = await fetch_click_page(url, page, current_ct, page_num)
            elif current_ct == 'p':
                html = fetch_static(page_url or url)  # POST는 정적으로 간소화
            else:
                logger.warning(f"[알 수 없는 타입] {site_name}: '{current_ct}' → fallback")
                html = fetch_static(page_url or url)

            if not html:
                if page_num == 1:
                    error_msg = "HTML 수집 실패"
                    status = "실패"
                break

            # ── 1순위: 셀렉터 파싱 ──
            items = parse_with_selector(html, site)
            used_method = "selector"

            # ── 2순위: 셀렉터 실패 시 Claude/regex fallback ──
            if not items:
                if page_num == 1:
                    # HTML 샘플 로깅 (디버그용)
                    soup_dbg = BeautifulSoup(html, 'html.parser')
                    title_tag = soup_dbg.find('title')
                    page_title = title_tag.get_text(strip=True)[:60] if title_tag else 'no-title'
                    logger.debug(f"[셀렉터 0건] {site_name} html={len(html)}b title='{page_title}'")
                text = preprocess_html(html)
                if text and len(text) >= 30:
                    items = extract_with_claude(site_name, text)
                    used_method = "claude" if _claude_available else "regex"
                    if items:
                        logger.info(f"[{used_method} fallback] {site_name} p{page_num}: {len(items)}건")
                    else:
                        if page_num == 1:
                            error_msg = "공고 추출 0건"
                            status = "실패"
                        break
                else:
                    if page_num == 1:
                        error_msg = "본문 추출 실패"
                        status = "실패"
                    break

            if page_num == 1:
                method = used_method
            logger.info(f"[추출] {site_name} p{page_num}: {len(items)}건 ({used_method})")

            for item in items:
                date_str = fix_date_format(item.get('date', ''))
                all_dates.append(date_str)
                all_items.append({
                    'site_id': site_id, 'site_name': site_name,
                    'title': item['title'], 'date': date_str, 'url': url,
                })
                if is_recent(date_str):
                    matched, kw = matches_keyword(item['title'])
                    if matched:
                        collected.append({
                            'site_id': site_id, 'site_name': site_name,
                            'title': item['title'], 'date': date_str,
                            'url': url, 'keyword': kw,
                            'region': extract_region(site_name),
                        })

            # 고유 날짜 3개 이상이면 충분히 수집됨
            unique = len(set(d for d in all_dates if d))
            if unique >= 3:
                break
            # 클릭 기반 페이지네이션은 page 2부터 current_ct로 처리
            # URL 기반 V2: update_url_for_page가 처리

    except Exception as e:
        error_msg = str(e)[:100]
        status = "실패"
        logger.error(f"[오류] {site_name}: {e}")
    finally:
        if page:
            try:
                await page.context.close()
            except Exception:
                pass

    logger.info(f"[완료] {site_name}: 키워드 {len(collected)}건 / 전체 {len(all_items)}건 ({method})")
    return {
        'site_id': site_id, 'site_name': site_name,
        'status': status, 'method': method,
        'items': collected, 'all_items': all_items, 'error_msg': error_msg,
    }

def _skip_result(site):
    return {
        'site_id': site['id'], 'site_name': site['name'],
        'status': f"스킵({site.get('skip_reason','')})",
        'method': 'skip', 'items': [], 'all_items': [], 'error_msg': '',
    }

# ─────────────────────────────────────────
# 병렬 실행
# ─────────────────────────────────────────
async def run_all(sites: list[dict]) -> list[dict]:
    skip_sites    = [s for s in sites if s.get('skip')]
    static_sites  = [s for s in sites if not s.get('skip') and not s['dynamic'] and s.get('crawl_type','s') == 's']
    dynamic_sites = [s for s in sites if not s.get('skip') and (s['dynamic'] or s.get('crawl_type','s') != 's')]

    results = [_skip_result(s) for s in skip_sites]

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=['--no-sandbox','--disable-dev-shm-usage','--disable-gpu','--disable-extensions']
        )

        sem_s = asyncio.Semaphore(STATIC_CONC)
        sem_d = asyncio.Semaphore(DYNAMIC_CONC)

        async def _do_static(site):
            async with sem_s:
                return await crawl_site(site, browser=None)

        async def _do_dynamic(site):
            async with sem_d:
                return await crawl_site(site, browser=browser)

        tasks = [_do_static(s) for s in static_sites] + [_do_dynamic(s) for s in dynamic_sites]
        for coro in asyncio.as_completed(tasks):
            results.append(await coro)

        await browser.close()

    return results

# ─────────────────────────────────────────
# Google Sheets
# ─────────────────────────────────────────
def get_gspread():
    if not GOOGLE_CREDENTIALS_JSON or not GOOGLE_SHEET_ID:
        return None, None
    try:
        creds = Credentials.from_service_account_info(
            json.loads(GOOGLE_CREDENTIALS_JSON),
            scopes=['https://www.googleapis.com/auth/spreadsheets']
        )
        gc = gspread.authorize(creds)
        return gc, gc.open_by_key(GOOGLE_SHEET_ID)
    except Exception as e:
        logger.error(f"[Google Sheets 연결 실패] {e}")
        return None, None

def upload_keyword_tab(sh, rows: list):
    """✅키워드공고 탭 - v4 호환 형식 (HYPERLINK, 키워드별 Y/N, 경과일, 확인여부/비고 보존)"""
    if not sh or not rows:
        return
    import pandas as pd
    tab_name = '✅키워드공고'
    try:
        try:
            ws = sh.worksheet(tab_name)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(tab_name, rows=5000, cols=30)

        existing = pd.DataFrame(ws.get_all_records())
        new_df   = pd.DataFrame(rows, columns=['지역','출처','제목','작성일','URL','키워드','수집일','확인여부','비고'])

        # 기존 확인여부/비고 보존
        preserved = {}
        if not existing.empty:
            for _, row in existing.iterrows():
                key = (str(row.get('출처','')), str(row.get('제목','')))
                preserved[key] = {'확인여부': row.get('확인여부',''), '비고': row.get('비고','')}

        # 병합 + 중복 제거
        if not existing.empty and '출처' in existing.columns and '제목' in existing.columns:
            combined = pd.concat([existing, new_df], ignore_index=True)
            combined = combined.drop_duplicates(subset=['출처','제목'], keep='last')
        else:
            combined = new_df.copy()

        # 확인여부/비고 복원
        for idx, row in combined.iterrows():
            key = (str(row.get('출처','')), str(row.get('제목','')))
            meta = preserved.get(key, {})
            if meta.get('확인여부'): combined.at[idx, '확인여부'] = meta['확인여부']
            if meta.get('비고'):     combined.at[idx, '비고'] = meta['비고']

        # 경과일
        today_dt = now_kst().date()
        def days_elapsed(d):
            try: return (today_dt - pd.to_datetime(d, errors='coerce').date()).days
            except: return ''
        combined['경과일'] = combined['작성일'].apply(days_elapsed)

        # 키워드별 Y/N
        for kw in FILTER_KEYWORDS:
            combined[kw] = combined['키워드'].apply(lambda v: 'Y' if kw in str(v) else '')

        # 작성일 내림차순 정렬
        combined['작성일_sort'] = pd.to_datetime(combined['작성일'], errors='coerce')
        combined = combined.sort_values('작성일_sort', ascending=False).drop(columns=['작성일_sort'])

        # 원문링크 HYPERLINK 수식
        combined['원문링크'] = combined.apply(
            lambda r: f'=HYPERLINK("{r["URL"]}","{str(r["제목"])[:30].replace(chr(34),"")}") '
            if str(r.get('URL','')).startswith('http') else '', axis=1)

        combined = combined.fillna('').astype(str)
        ws.clear()
        ws.update([combined.columns.tolist()] + combined.values.tolist(), value_input_option='USER_ENTERED')
        logger.info(f"[업로드] '{tab_name}': {len(combined)}행")
    except Exception as e:
        logger.error(f"[업로드 실패] '{tab_name}': {e}")


def upload_tab(sh, tab_name: str, rows: list, headers: list):
    if not sh or not rows:
        return
    import pandas as pd
    try:
        try:
            ws = sh.worksheet(tab_name)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(tab_name, rows=5000, cols=30)

        existing = pd.DataFrame(ws.get_all_records())
        new_df   = pd.DataFrame(rows, columns=headers)

        if not existing.empty and '출처' in existing.columns and '제목' in existing.columns:
            combined = pd.concat([existing, new_df], ignore_index=True)
            combined = combined.drop_duplicates(subset=['출처','제목'], keep='last')
        else:
            combined = new_df

        combined = combined.fillna('').astype(str)
        ws.clear()
        ws.update([combined.columns.tolist()] + combined.values.tolist(), value_input_option='USER_ENTERED')
        logger.info(f"[업로드] '{tab_name}': {len(combined)}행")
    except Exception as e:
        logger.error(f"[업로드 실패] '{tab_name}': {e}")


def upload_log_cumulative(sh, rows: list):
    """크롤링로그 탭 - 30일 누적 방식 (v4 호환)"""
    if not sh:
        return
    import pandas as pd
    tab_name = '크롤링로그'
    try:
        try:
            ws = sh.worksheet(tab_name)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(tab_name, rows=10000, cols=20)

        existing = pd.DataFrame(ws.get_all_records())
        new_df   = pd.DataFrame(rows, columns=['사이트ID','사이트명','상태','수집건수','오류메시지','추출방식','실행시각'])

        combined = pd.concat([existing, new_df], ignore_index=True) if not existing.empty else new_df
        # 30일치 보존
        if '실행시각' in combined.columns:
            combined['_dt'] = pd.to_datetime(combined['실행시각'], errors='coerce')
            cutoff = pd.Timestamp(now_kst()) - pd.Timedelta(days=30)
            combined = combined[combined['_dt'] >= cutoff].drop(columns=['_dt'])
        combined = combined.fillna('').astype(str)
        ws.clear()
        ws.update([combined.columns.tolist()] + combined.values.tolist(), value_input_option='USER_ENTERED')
        logger.info(f"[업로드] '{tab_name}': {len(combined)}행 (누적)")
    except Exception as e:
        logger.error(f"[업로드 실패] '{tab_name}': {e}")

# ─────────────────────────────────────────
# SQLite 저장
# ─────────────────────────────────────────
def save_to_db(con, results: list[dict], run_at: str):
    cur = con.cursor()
    for r in results:
        cur.execute(
            "INSERT INTO crawl_log (run_at,site_id,site_name,status,item_count,error_msg,method) VALUES (?,?,?,?,?,?,?)",
            (run_at, r['site_id'], r['site_name'], r['status'], len(r['items']), r['error_msg'], r.get('method',''))
        )
        for item in r.get('all_items', []):
            try:
                cur.execute(
                    "INSERT OR IGNORE INTO announcements (site_id,site_name,title,date,url,keyword,collected_at) VALUES (?,?,?,?,?,?,?)",
                    (item['site_id'], item['site_name'], item['title'], item.get('date',''), item.get('url',''), item.get('keyword',''), run_at)
                )
            except Exception:
                pass
    con.commit()

# ─────────────────────────────────────────
# 메인
# ─────────────────────────────────────────
async def main():
    run_at    = now_kst().strftime('%Y-%m-%d %H:%M:%S KST')
    today_str = now_kst().strftime('%Y%m%d')
    logger.info(f"===== crawl_v5 시작 (Hybrid): {run_at} =====")

    with open(SITES_FILE, encoding='utf-8') as f:
        sites = json.load(f)

    active = [s for s in sites if not s.get('skip')]
    logger.info(f"전체 {len(sites)}개 | 활성 {len(active)}개 | 스킵 {len(sites)-len(active)}개")

    con = init_db()
    results = await run_all(sites)

    success = sum(1 for r in results if r['status'] == '성공')
    failed  = sum(1 for r in results if '실패' in r['status'])
    skipped = sum(1 for r in results if '스킵' in r['status'])
    by_method = {}
    for r in results:
        m = r.get('method','')
        by_method[m] = by_method.get(m, 0) + 1

    logger.info(f"===== 결과: 성공 {success} | 실패 {failed} | 스킵 {skipped} / 전체 {len(results)} =====")
    logger.info(f"추출 방식: {by_method}")

    save_to_db(con, results, run_at)
    con.close()

    keyword_rows, all_rows, log_rows = [], [], []
    for r in results:
        log_rows.append([r['site_id'], r['site_name'], r['status'],
                         len(r['items']), r['error_msg'], r.get('method',''), run_at])
        for item in r.get('items', []):
            keyword_rows.append([item.get('region',''), item['site_name'], item['title'],
                                  item.get('date',''), item.get('url',''),
                                  item.get('keyword',''), run_at, '', ''])
        for item in r.get('all_items', []):
            if not item.get('keyword'):
                all_rows.append([item['site_name'], item['title'],
                                  item.get('date',''), item.get('url',''), run_at])

    _, sh = get_gspread()
    upload_keyword_tab(sh, keyword_rows)
    upload_tab(sh, '📋전체공고(키워드제외)',
               all_rows,     ['출처','제목','작성일','URL','수집일'])
    upload_log_cumulative(sh, log_rows)

    import pandas as pd
    if keyword_rows:
        pd.DataFrame(keyword_rows,
            columns=['지역','출처','제목','작성일','URL','키워드','수집일','확인여부','비고']
        ).to_excel(f'df_list_v5_{today_str}.xlsx', index=False)
    pd.DataFrame(log_rows,
        columns=['사이트ID','사이트명','상태','수집건수','오류메시지','추출방식','실행시각']
    ).to_excel(f'df_log_v5_{today_str}.xlsx', index=False)

    logger.info(f"===== 완료: 키워드공고 {len(keyword_rows)}건 =====")

if __name__ == '__main__':
    asyncio.run(main())
