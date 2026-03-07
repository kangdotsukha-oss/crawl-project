"""
공공기관 고시/공고 자동 크롤러 v5 - Zero-Selector 버전
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
변경사항 (vs v4):
  - CSS 셀렉터 완전 제거 → Claude Haiku가 HTML에서 직접 추출
  - Selenium → Playwright async (속도↑, 안정성↑)
  - trafilatura 전처리 → 토큰 10배 절감
  - sites.json (URL+dynamic 여부만) → 12컬럼 Excel 대체
  - SQLite 로컬 저장 → git push race condition 제거
  - Google Sheets는 표시 전용

비용: Claude Haiku ~$30-50/월 (229사이트×2회/일)
복구: git checkout main  (v4-stable 태그로 언제든 되돌리기 가능)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import anthropic
import gspread
import trafilatura
from google.oauth2.service_account import Credentials
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ─────────────────────────────────────────
# 환경변수 (.env 지원)
# ─────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

ANTHROPIC_API_KEY    = os.environ.get("ANTHROPIC_API_KEY", "")
GOOGLE_SHEET_ID      = os.environ.get("GOOGLE_SHEET_ID", "")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")

# ─────────────────────────────────────────
# 상수
# ─────────────────────────────────────────
KST          = timezone(timedelta(hours=9))
DAYS_RANGE   = 1          # 최근 N일치 공고 수집
MAX_PAGES    = 3          # 페이지 최대 탐색 수
STATIC_CONC  = 10         # 정적 사이트 동시 처리 수
DYNAMIC_CONC = 4          # 동적 사이트 동시 처리 수
PAGE_TIMEOUT = 30_000     # ms
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
_sh.stream = open(_sh.stream.fileno(), mode='w', encoding='utf-8', closefd=False) if hasattr(_sh.stream, 'fileno') else _sh.stream
logging.basicConfig(level=logging.INFO, handlers=[_fh, _sh])
logger = logging.getLogger(__name__)

def now_kst():
    return datetime.now(KST)

# ─────────────────────────────────────────
# SQLite 초기화
# ─────────────────────────────────────────
def init_db():
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS announcements (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            site_id     TEXT,
            site_name   TEXT,
            title       TEXT,
            date        TEXT,
            url         TEXT,
            keyword     TEXT,
            collected_at TEXT,
            UNIQUE(site_name, title, date)
        );
        CREATE TABLE IF NOT EXISTS crawl_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at      TEXT,
            site_id     TEXT,
            site_name   TEXT,
            status      TEXT,
            item_count  INTEGER,
            error_msg   TEXT
        );
    """)
    con.commit()
    return con

# ─────────────────────────────────────────
# Claude Haiku - 공고 추출
# ─────────────────────────────────────────
_claude_client: Optional[anthropic.Anthropic] = None

def get_claude():
    global _claude_client
    if _claude_client is None:
        _claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _claude_client

# Claude API 사용 가능 여부 캐시 (크레딧 소진 시 빠른 fallback 전환)
_claude_available: bool = True

def extract_with_claude(site_name: str, text: str) -> list[dict]:
    """
    trafilatura로 전처리된 텍스트에서 공고 목록 추출.
    Claude API 실패(크레딧 부족 등) 시 regex fallback 자동 전환.
    반환: [{"title": "...", "date": "YYYY-MM-DD"}, ...]
    """
    global _claude_available

    if not text or len(text.strip()) < 50:
        return []

    # Claude 가능하면 Claude 우선 시도
    if ANTHROPIC_API_KEY and _claude_available:
        result = _extract_claude(site_name, text)
        if result is not None:
            return result
        # None 반환 = 크레딧 소진 등 비복구 오류 → fallback 전환
        logger.warning(f"[fallback 전환] Claude 사용 불가 → regex fallback")
        _claude_available = False

    # Regex fallback
    return _extract_regex(text)


def _extract_claude(site_name: str, text: str) -> list[dict] | None:
    """Claude 추출. 성공 시 list, 크레딧 부족 등 비복구 오류 시 None."""
    prompt = f"""다음은 한국 공공기관 고시/공고 목록 페이지의 텍스트입니다.
사이트명: {site_name}

텍스트:
{text[:6000]}

위 텍스트에서 고시/공고 항목들을 추출하세요.
- 각 항목의 제목(title)과 날짜(date)를 추출
- 날짜는 YYYY-MM-DD 형식으로 정규화 (예: 2026-03-07)
- 날짜를 알 수 없으면 "" 로 표시
- 공고가 없으면 빈 배열 반환
- 다른 설명 없이 JSON 배열만 출력

출력 형식:
[
  {{"title": "공고 제목", "date": "YYYY-MM-DD"}},
  ...
]"""

    try:
        response = get_claude().messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()
        match = re.search(r'\[.*\]', raw, re.DOTALL)
        if match:
            items = json.loads(match.group())
            return [i for i in items if isinstance(i, dict) and i.get('title')]
        return []
    except anthropic.BadRequestError as e:
        # 크레딧 부족 / 잘못된 요청 → 비복구, None 반환
        if 'credit' in str(e).lower() or 'balance' in str(e).lower():
            logger.warning(f"[Claude 크레딧 부족] fallback으로 전환합니다")
            return None
        logger.warning(f"[Claude 오류] {site_name}: {e}")
        return []
    except Exception as e:
        logger.warning(f"[Claude 오류] {site_name}: {e}")
        return []


# 날짜 정규화 패턴
_DATE_PATTERNS = [
    re.compile(r'(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})'),  # 2026-03-07
    re.compile(r'(\d{4})(\d{2})(\d{2})'),                    # 20260307
]
_KO_TITLE_RE = re.compile(r'[\uAC00-\uD7A3]{2,}[^\n\t]{3,60}')

def _extract_regex(text: str) -> list[dict]:
    """
    Claude 없이 regex로 제목/날짜 추출하는 fallback.
    공고 목록 특성상 날짜 근처 줄이 제목일 가능성이 높음.
    """
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    items = []
    seen_titles = set()

    for i, line in enumerate(lines):
        date_str = ""
        for pat in _DATE_PATTERNS:
            m = pat.search(line)
            if m:
                try:
                    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                    if 2020 <= y <= 2030 and 1 <= mo <= 12 and 1 <= d <= 31:
                        date_str = f"{y:04d}-{mo:02d}-{d:02d}"
                        break
                except Exception:
                    pass

        if not date_str:
            continue

        # 제목: 같은 줄 또는 앞 줄에서 한글 포함 텍스트 탐색
        title = ""
        for offset in [0, -1, -2, 1]:
            idx = i + offset
            if 0 <= idx < len(lines):
                candidate = lines[idx]
                # 날짜만 있는 줄, 숫자만 있는 줄 제외
                if _KO_TITLE_RE.search(candidate) and not re.fullmatch(r'[\d\s.\-/|]+', candidate):
                    # 날짜 패턴만으로 이루어진 줄 제외
                    clean = re.sub(r'\d{4}[.\-/]\d{1,2}[.\-/]\d{1,2}', '', candidate).strip()
                    if len(clean) >= 5:
                        title = clean[:100]
                        break

        if title and title not in seen_titles:
            # 테이블 구분자(|), 앞 번호, 조회수 정리
            title = re.sub(r'\|\s*\d+\s*\|?', '', title)
            title = re.sub(r'^\s*\d+\s+', '', title)
            title = re.sub(r'\s{2,}', ' ', title).strip('| \t')
            if len(title) < 5:
                continue
            seen_titles.add(title)
            items.append({"title": title, "date": date_str})

    return items[:30]

# ─────────────────────────────────────────
# HTML 전처리 (trafilatura)
# ─────────────────────────────────────────
def preprocess_html(html: str) -> str:
    """HTML → 깔끔한 텍스트 (script/style 제거, 본문 추출)"""
    if not html:
        return ""
    # 1차: trafilatura
    text = trafilatura.extract(
        html,
        include_tables=True,
        include_links=False,
        include_images=False,
        no_fallback=False,
        favor_recall=True,
    )
    # 2차: BeautifulSoup fallback (테이블 구조 보존)
    if not text or len(text) < 100:
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, 'html.parser')
            for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'aside']):
                tag.decompose()
            for td in soup.find_all('td'):
                td.insert_after('\t')
            for tr in soup.find_all('tr'):
                tr.insert_after('\n')
            fallback = soup.get_text(separator='\n', strip=True)
            fallback = re.sub(r'\n{3,}', '\n\n', fallback)
            if len(fallback) > len(text or ''):
                text = fallback
        except Exception:
            pass
    return (text or '')[:8000]

# ─────────────────────────────────────────
# 정적 크롤링 (requests)
# ─────────────────────────────────────────
import requests
from requests.packages.urllib3.exceptions import InsecureRequestWarning
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

UA_LIST = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

def fetch_static(url: str, ua_idx: int = 0) -> Optional[str]:
    headers = {
        "User-Agent": UA_LIST[ua_idx % len(UA_LIST)],
        "Accept-Language": "ko-KR,ko;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        r = requests.get(url, headers=headers, timeout=(15, 30), verify=False)
        r.raise_for_status()
        r.encoding = r.apparent_encoding or 'utf-8'
        return r.text
    except Exception as e:
        logger.debug(f"[정적 실패] {url}: {e}")
        return None

# ─────────────────────────────────────────
# 동적 크롤링 (Playwright)
# ─────────────────────────────────────────
async def fetch_dynamic(url: str, page) -> Optional[str]:
    try:
        await page.goto(url, wait_until='domcontentloaded', timeout=PAGE_TIMEOUT)
        # 네트워크 안정화 대기
        try:
            await page.wait_for_load_state('networkidle', timeout=5000)
        except Exception:
            pass
        await asyncio.sleep(2)
        return await page.content()
    except PlaywrightTimeout:
        # 타임아웃 시에도 현재 렌더링된 내용 반환 시도
        try:
            return await page.content()
        except Exception:
            return None
    except Exception as e:
        logger.debug(f"[동적 실패] {url}: {e}")
        return None

# ─────────────────────────────────────────
# 날짜 필터
# ─────────────────────────────────────────
def is_recent(date_str: str, days: int = DAYS_RANGE) -> bool:
    """date_str이 최근 N일 이내인지 확인"""
    if not date_str:
        return True  # 날짜 불명 → 포함
    try:
        dt = datetime.strptime(date_str[:10], '%Y-%m-%d')
        cutoff = now_kst().replace(tzinfo=None) - timedelta(days=days)
        return dt >= cutoff
    except Exception:
        return True

def matches_keyword(title: str) -> tuple[bool, str]:
    """키워드 매칭 여부 및 매칭된 키워드 반환"""
    for kw in FILTER_KEYWORDS:
        if kw in title:
            return True, kw
    return False, ""

# ─────────────────────────────────────────
# 지역 분류
# ─────────────────────────────────────────
REGION_MAP = [
    ('서울', '서울'), ('부산', '부산'), ('대구', '대구'), ('인천', '인천'),
    ('광주', '광주'), ('대전', '대전'), ('울산', '울산'), ('세종', '세종'),
    ('경기', '경기도'), ('강원', '강원도'),
    ('충북', '충청도'), ('충남', '충청도'), ('충청', '충청도'),
    ('전북', '전라도'), ('전남', '전라도'), ('전라', '전라도'),
    ('경북', '경상도'), ('경남', '경상도'), ('경상', '경상도'),
    ('제주', '제주도'),
]

def extract_region(name: str) -> str:
    for key, region in REGION_MAP:
        if key in name:
            return region
    return '기타'

# ─────────────────────────────────────────
# 단일 사이트 크롤링
# ─────────────────────────────────────────
async def crawl_site(site: dict, browser=None) -> dict:
    site_id   = site['id']
    site_name = site['name']
    url       = site['url']
    dynamic   = site['dynamic']
    collected = []
    all_items = []
    error_msg = ""
    status    = "성공"

    if site.get('skip'):
        return {
            'site_id': site_id, 'site_name': site_name,
            'status': f"스킵({site.get('skip_reason','')})",
            'items': [], 'all_items': [], 'error_msg': ''
        }

    logger.info(f"[시작] {site_name}")
    page = None

    try:
        if dynamic and browser:
            context = await browser.new_context(
                user_agent=UA_LIST[0],
                locale='ko-KR',
            )
            page = await context.new_page()

        all_dates = []

        for page_num in range(1, MAX_PAGES + 1):
            # 페이지 URL 업데이트
            page_url = _update_page_url(url, page_num)
            if page_url is None:
                break

            # HTML 수집
            html = None
            if dynamic and page:
                html = await fetch_dynamic(page_url, page)
            else:
                html = fetch_static(page_url)

            if not html:
                # 정적 실패 시 UA 교체 재시도
                for ua_idx in range(1, len(UA_LIST)):
                    html = fetch_static(page_url, ua_idx)
                    if html:
                        break

            if not html:
                if page_num == 1:
                    error_msg = "HTML 수집 실패"
                    status = "실패"
                break

            # 전처리
            text = preprocess_html(html)
            if not text or len(text) < 30:
                if page_num == 1:
                    error_msg = "본문 추출 실패"
                    status = "실패"
                break

            # Claude로 추출
            items = extract_with_claude(site_name, text)
            if not items and page_num == 1:
                error_msg = "공고 추출 0건"
                status = "실패"
                break

            logger.info(f"[추출] {site_name} p{page_num}: {len(items)}건")

            # 날짜 수집 (페이지 이동 판단용)
            for item in items:
                date_str = item.get('date', '')
                all_dates.append(date_str)
                all_items.append({
                    'site_id': site_id,
                    'site_name': site_name,
                    'title': item['title'],
                    'date': date_str,
                    'url': url,
                })

            # 최근 N일치만 필터
            recent = [i for i in items if is_recent(i.get('date', ''))]
            for item in recent:
                matched, kw = matches_keyword(item['title'])
                if matched:
                    collected.append({
                        'site_id': site_id,
                        'site_name': site_name,
                        'title': item['title'],
                        'date': item.get('date', ''),
                        'url': url,
                        'keyword': kw,
                        'region': extract_region(site_name),
                    })

            # 다음 페이지 이동 여부: 고유 날짜 3개 이상이면 충분히 수집
            unique_dates = len(set(d for d in all_dates if d))
            if unique_dates >= 3 or not _has_next_page_param(url):
                break

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

    logger.info(f"[완료] {site_name}: 키워드매칭 {len(collected)}건 / 전체 {len(all_items)}건")
    return {
        'site_id': site_id,
        'site_name': site_name,
        'status': status,
        'items': collected,
        'all_items': all_items,
        'error_msg': error_msg,
    }

# ─────────────────────────────────────────
# URL 페이지네이션 헬퍼
# ─────────────────────────────────────────
_PAGE_PARAMS = [
    'pageIndex', 'page', 'Page', 'pageNo', 'cpn', 'pageid', 'p',
]

def _has_next_page_param(url: str) -> bool:
    return any(p + '=' in url for p in _PAGE_PARAMS)

def _update_page_url(url: str, page_num: int) -> Optional[str]:
    if page_num == 1:
        return url
    for param in _PAGE_PARAMS:
        pattern = rf'({re.escape(param)}=)\d+'
        if re.search(pattern, url):
            return re.sub(pattern, rf'\g<1>{page_num}', url)
    # offset 패턴 (offset=0, 15, 30...)
    if 'offset=' in url:
        return re.sub(r'offset=\d+', f'offset={(page_num-1)*15}', url)
    # Start= 패턴
    if 'Start=' in url:
        return re.sub(r'Start=\d+', f'Start={(page_num-1)*10}', url)
    # 페이지 파라미터 없으면 None (더 이상 순회 안 함)
    return None

# ─────────────────────────────────────────
# 병렬 실행
# ─────────────────────────────────────────
async def run_all(sites: list[dict]) -> list[dict]:
    static_sites  = [s for s in sites if not s.get('skip') and not s['dynamic']]
    dynamic_sites = [s for s in sites if not s.get('skip') and s['dynamic']]
    skip_sites    = [s for s in sites if s.get('skip')]

    results = []

    # 스킵 사이트 처리
    for site in skip_sites:
        results.append({
            'site_id': site['id'], 'site_name': site['name'],
            'status': f"스킵({site.get('skip_reason','')})",
            'items': [], 'all_items': [], 'error_msg': ''
        })

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-dev-shm-usage',
                  '--disable-gpu', '--disable-extensions']
        )

        # 정적 사이트: asyncio.Semaphore로 동시성 제어
        sem_static = asyncio.Semaphore(STATIC_CONC)
        async def _crawl_static(site):
            async with sem_static:
                return await crawl_site(site, browser=None)

        # 동적 사이트: 동시성 낮게 (브라우저 컨텍스트 비용)
        sem_dynamic = asyncio.Semaphore(DYNAMIC_CONC)
        async def _crawl_dynamic(site):
            async with sem_dynamic:
                return await crawl_site(site, browser=browser)

        static_tasks  = [_crawl_static(s)  for s in static_sites]
        dynamic_tasks = [_crawl_dynamic(s) for s in dynamic_sites]

        all_tasks = static_tasks + dynamic_tasks
        for coro in asyncio.as_completed(all_tasks):
            result = await coro
            results.append(result)

        await browser.close()

    return results

# ─────────────────────────────────────────
# Google Sheets 업로드
# ─────────────────────────────────────────
def get_gspread():
    if not GOOGLE_CREDENTIALS_JSON or not GOOGLE_SHEET_ID:
        return None, None
    try:
        creds_info = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = Credentials.from_service_account_info(
            creds_info,
            scopes=['https://www.googleapis.com/auth/spreadsheets']
        )
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(GOOGLE_SHEET_ID)
        return gc, sh
    except Exception as e:
        logger.error(f"[Google Sheets 연결 실패] {e}")
        return None, None

def upload_to_sheets(sh, keyword_rows: list, all_rows: list, log_rows: list):
    if not sh:
        return

    def _upload_tab(tab_name: str, rows: list, headers: list):
        try:
            try:
                ws = sh.worksheet(tab_name)
            except gspread.WorksheetNotFound:
                ws = sh.add_worksheet(tab_name, rows=5000, cols=30)

            if not rows:
                logger.info(f"[업로드 스킵] '{tab_name}': 데이터 없음")
                return

            import pandas as pd
            existing_df = pd.DataFrame(ws.get_all_records())
            new_df = pd.DataFrame(rows, columns=headers)

            # 기존 데이터와 병합 후 중복 제거
            if not existing_df.empty and '출처' in existing_df.columns and '제목' in existing_df.columns:
                combined = pd.concat([existing_df, new_df], ignore_index=True)
                combined = combined.drop_duplicates(subset=['출처', '제목'], keep='last')
            else:
                combined = new_df

            combined = combined.fillna('').astype(str)
            ws.clear()
            ws.update(
                [combined.columns.tolist()] + combined.values.tolist(),
                value_input_option='USER_ENTERED'
            )
            logger.info(f"[업로드 완료] '{tab_name}': {len(combined)}행")
        except Exception as e:
            logger.error(f"[업로드 실패] '{tab_name}': {e}")

    _upload_tab(
        '✅키워드공고',
        keyword_rows,
        ['지역', '출처', '제목', '날짜', 'URL', '키워드', '수집일', '확인여부', '비고']
    )
    _upload_tab(
        '📋전체공고(키워드제외)',
        all_rows,
        ['출처', '제목', '날짜', 'URL', '수집일']
    )
    _upload_tab(
        '크롤링로그',
        log_rows,
        ['사이트ID', '사이트명', '상태', '수집건수', '오류메시지', '실행시각']
    )

# ─────────────────────────────────────────
# SQLite 저장
# ─────────────────────────────────────────
def save_to_db(con: sqlite3.Connection, results: list[dict], run_at: str):
    cur = con.cursor()
    for r in results:
        # 로그
        cur.execute(
            "INSERT INTO crawl_log (run_at, site_id, site_name, status, item_count, error_msg) VALUES (?,?,?,?,?,?)",
            (run_at, r['site_id'], r['site_name'], r['status'],
             len(r['items']), r['error_msg'])
        )
        # 공고 저장 (UNIQUE 중복 무시)
        for item in r.get('all_items', []):
            try:
                cur.execute(
                    "INSERT OR IGNORE INTO announcements "
                    "(site_id, site_name, title, date, url, keyword, collected_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (item['site_id'], item['site_name'], item['title'],
                     item.get('date',''), item.get('url',''),
                     item.get('keyword',''), run_at)
                )
            except Exception:
                pass
    con.commit()

# ─────────────────────────────────────────
# 메인
# ─────────────────────────────────────────
async def main():
    run_at = now_kst().strftime('%Y-%m-%d %H:%M:%S KST')
    today_str = now_kst().strftime('%Y%m%d')
    logger.info(f"===== crawl_v5 시작: {run_at} =====")

    # 사이트 목록 로드
    with open(SITES_FILE, encoding='utf-8') as f:
        sites = json.load(f)
    logger.info(f"사이트 {len(sites)}개 로드 (활성: {sum(1 for s in sites if not s.get('skip'))}개)")

    # DB 초기화
    con = init_db()

    # 크롤링 실행
    results = await run_all(sites)

    # 결과 집계
    success = sum(1 for r in results if r['status'] == '성공')
    failed  = sum(1 for r in results if '실패' in r['status'])
    skipped = sum(1 for r in results if '스킵' in r['status'])

    logger.info(f"===== 결과: 성공 {success} | 실패 {failed} | 스킵 {skipped} / 전체 {len(results)} =====")

    # DB 저장
    save_to_db(con, results, run_at)
    con.close()

    # Google Sheets용 데이터 준비
    keyword_rows = []
    all_rows = []
    log_rows = []

    cutoff = (now_kst() - timedelta(days=DAYS_RANGE)).replace(tzinfo=None)

    for r in results:
        log_rows.append([
            r['site_id'], r['site_name'], r['status'],
            len(r['items']), r['error_msg'], run_at
        ])
        for item in r.get('items', []):
            keyword_rows.append([
                item.get('region',''), item['site_name'], item['title'],
                item.get('date',''), item.get('url',''),
                item.get('keyword',''), run_at, '', ''
            ])
        for item in r.get('all_items', []):
            if item.get('keyword'):
                continue  # 키워드 탭에 이미 있음
            all_rows.append([
                item['site_name'], item['title'],
                item.get('date',''), item.get('url',''), run_at
            ])

    # Google Sheets 업로드
    _, sh = get_gspread()
    upload_to_sheets(sh, keyword_rows, all_rows, log_rows)

    # Excel 백업 (기존 방식 호환)
    import pandas as pd
    if keyword_rows:
        pd.DataFrame(keyword_rows,
            columns=['지역','출처','제목','날짜','URL','키워드','수집일','확인여부','비고']
        ).to_excel(f'df_list_v5_{today_str}.xlsx', index=False)
    pd.DataFrame(log_rows,
        columns=['사이트ID','사이트명','상태','수집건수','오류메시지','실행시각']
    ).to_excel(f'df_log_v5_{today_str}.xlsx', index=False)

    logger.info(f"===== crawl_v5 완료: 키워드공고 {len(keyword_rows)}건 =====")

if __name__ == '__main__':
    asyncio.run(main())
