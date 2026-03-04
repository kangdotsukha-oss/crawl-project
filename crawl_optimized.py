"""
공공기관 고시/공고 자동 크롤러 (최적화 버전)
- 중복 함수 통합 (click_dynamic_crawl 1~34 → 단일 함수)
- 병렬 크롤링 (ThreadPoolExecutor)
- 에러 처리 강화
- 로깅 개선
- Google Sheets 자동 업로드
- GitHub Actions 스케줄링 지원
"""

import requests
from bs4 import BeautifulSoup
import pandas as pd
import numpy as np
import re
import logging
import time
import gspread
import os
from requests.packages.urllib3.exceptions import InsecureRequestWarning
from tqdm import tqdm
from datetime import datetime, timedelta
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from concurrent.futures import ThreadPoolExecutor, as_completed
from google.oauth2.service_account import Credentials

# ─────────────────────────────────────────────
# 로깅 설정
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("crawl.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# 상수 설정
# ─────────────────────────────────────────────
MAX_RETRIES = 2
MAX_PAGES = 9
FILTER_KEYWORDS = ['특허', '제안', '심의', '공법', '실시설계', '보수보강']
DAYS_RANGE = 5
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7"
}

# Google Sheets 설정 (GitHub Secrets 또는 로컬 .env에서 읽어옴)
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")          # 구글시트 ID
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON", "") # 서비스 계정 JSON 경로

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)


# ─────────────────────────────────────────────
# Selenium 드라이버 생성 (공통)
# ─────────────────────────────────────────────
def get_driver(timeout: int = 10) -> webdriver.Chrome:
    """Chrome 드라이버 생성 (GitHub Actions headless 지원)"""
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(timeout)
    return driver


# ─────────────────────────────────────────────
# 크롤링 함수 (통합)
# ─────────────────────────────────────────────
def static_crawl(row: pd.Series) -> BeautifulSoup | None:
    """정적 크롤링 (requests)"""
    try:
        response = requests.get(row['URL'], headers=HEADERS, timeout=(50, 50), verify=False)
        response.raise_for_status()
        response.encoding = 'utf-8'
        return BeautifulSoup(response.text, 'html.parser')
    except requests.exceptions.ConnectTimeout:
        logger.warning(f"[연결 타임아웃] {row['SITE_NAME']}")
    except requests.exceptions.ReadTimeout:
        logger.warning(f"[읽기 타임아웃] {row['SITE_NAME']}")
    except requests.exceptions.RequestException as e:
        logger.error(f"[요청 오류] {row['SITE_NAME']}: {e}")
    return None


def dynamic_crawl(row: pd.Series, wait: int = 10) -> BeautifulSoup:
    """동적 크롤링 - 단순 페이지 로드"""
    driver = get_driver()
    try:
        try:
            driver.get(row['URL'])
        except TimeoutException:
            logger.warning(f"[페이지 로딩 타임아웃] {row['SITE_NAME']}")
        time.sleep(wait)
        soup = BeautifulSoup(driver.page_source, 'html.parser')
    finally:
        driver.quit()
    return soup


def dynamic_crawl_1(row: pd.Series) -> BeautifulSoup:
    """동적 크롤링 - pageSize 드롭다운 클릭"""
    driver = get_driver()
    try:
        driver.get(row['URL'])
        time.sleep(10)
        driver.find_element(By.ID, 'ofr_pageSize').click()
        driver.find_element(By.XPATH, '//*[@id="ofr_pageSize"]/option[1]').click()
        time.sleep(3)
        soup = BeautifulSoup(driver.page_source, 'html.parser')
    finally:
        driver.quit()
    return soup


def dynamic_crawl_2(row: pd.Series) -> BeautifulSoup:
    """동적 크롤링 - CSS 셀렉터로 버튼 클릭"""
    driver = get_driver()
    try:
        driver.get(row['URL'])
        time.sleep(10)
        driver.find_element(By.CSS_SELECTOR, row['click_button']).click()
        time.sleep(3)
        soup = BeautifulSoup(driver.page_source, 'html.parser')
    finally:
        driver.quit()
    return soup


def post_crawl(row: pd.Series) -> BeautifulSoup:
    """POST 요청 크롤링"""
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
    response = requests.post(row['URL'], data=data).content
    return BeautifulSoup(response.decode('utf-8-sig'), 'html.parser')


# ─────────────────────────────────────────────
# 핵심 최적화: click_dynamic_crawl 1~34 → 단일 함수
# ─────────────────────────────────────────────

# 각 crawl_type별 셀렉터 패턴 정의
# selector_type: 'css' 또는 'xpath'
# selector: f-string 패턴 (page_number 변수 사용 가능)
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


def click_dynamic_crawl(row: pd.Series, page_number: int) -> BeautifulSoup:
    """
    통합 클릭 동적 크롤링 함수
    기존 click_dynamic_crawl1 ~ click_dynamic_crawl34 를 하나로 통합
    """
    crawl_type = row['crawl_type']
    config = CLICK_CRAWL_CONFIG.get(crawl_type)
    if not config:
        raise ValueError(f"알 수 없는 crawl_type: {crawl_type}")

    # f-string 패턴에서 page_number를 실제 값으로 치환
    raw_selector = config['selector']
    selector = eval(f'f"{raw_selector}"')
    wait_time = config.get('wait', 5)
    selector_type = config['type']

    logger.info(f"[클릭 크롤링] {row['SITE_NAME']} | 페이지 {page_number} | {selector_type}: {selector}")

    driver = get_driver()
    try:
        driver.get(row['URL'])
        time.sleep(wait_time)

        if page_number > 1:
            by = By.CSS_SELECTOR if selector_type == 'css' else By.XPATH
            try:
                btn = driver.find_element(by, selector)
                btn.click()
                time.sleep(wait_time)
            except NoSuchElementException:
                logger.warning(f"[버튼 없음] {row['SITE_NAME']} 페이지 {page_number} - 셀렉터: {selector}")

        soup = BeautifulSoup(driver.page_source, 'html.parser')
    finally:
        driver.quit()
    return soup


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

def update_url_for_next_page(url: str, page_number: int, div: str) -> str | None:
    if page_number == 1:
        return url
    if div != 'V2':
        logger.warning("URL 패턴 개발 중. 페이지 이동 불가.")
        return None
    for pattern, updater in URL_PATTERNS.items():
        if pattern in url:
            return updater(url, page_number)
    return url  # 패턴 없으면 원본 반환


def update_crawl_type(url: str, crawl_type: str, page_number: int, ct2) -> str:
    if page_number == 1:
        return crawl_type
    if not (isinstance(ct2, float) and np.isnan(ct2)):
        return ct2
    return crawl_type


# ─────────────────────────────────────────────
# 날짜 처리
# ─────────────────────────────────────────────
def fix_date_format(date_str: str) -> str:
    if not date_str or not isinstance(date_str, str):
        return date_str
    date_str = date_str.strip()
    if len(date_str) == 8 and date_str.isdigit():
        return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
    if len(date_str) == 18:
        return f"{date_str[:4]}-{date_str[5:7]}-{date_str[8:10]}"
    date_str = date_str.split('~')[0].strip()
    parts = date_str.split('-')
    if parts and len(parts[0]) == 2:
        return '20' + date_str
    return date_str


def extract_date_from_text(text: str, site_name: str) -> str:
    if '공고부서 :' in text:
        return text.split('공고부서 :')[-2].split('등록일 :')[-1].strip()
    elif '게재일 :' in text:
        return text.split('게재일 :')[1].strip()
    else:
        return text.replace('.', '-').replace('/', '-').replace('등록일 :', '').strip()


# ─────────────────────────────────────────────
# 단일 사이트 크롤링
# ─────────────────────────────────────────────
def crawl_site(row: pd.Series) -> dict:
    """
    단일 사이트 크롤링 수행
    반환: { 'data': [...], 'log': {...} }
    """
    site_name = row['SITE_NAME']
    cleaned_dates = []
    all_titles = []
    collected_data = []
    page_number = 1
    retries = 0

    logger.info(f"[시작] {site_name}")

    while page_number <= MAX_PAGES:
        try:
            updated_url = update_url_for_next_page(row['URL'], page_number, row['div'])
            if updated_url is None:
                logger.warning(f"[URL 업데이트 실패] {site_name} - 페이지 이동 중단")
                break
            row = row.copy()
            row['URL'] = updated_url
            row['crawl_type'] = update_crawl_type(row['URL'], row['crawl_type'], page_number, row['ct2'])

            success = False
            soup = None

            while retries < MAX_RETRIES and not success:
                try:
                    ct = row['crawl_type']
                    if ct == 's':
                        soup = static_crawl(row)
                    elif ct == 'd':
                        soup = dynamic_crawl(row)
                    elif ct == 'd1':
                        soup = dynamic_crawl_1(row)
                    elif ct == 'd2':
                        soup = dynamic_crawl_2(row)
                    elif ct == 'p':
                        soup = post_crawl(row)
                    elif ct in CLICK_CRAWL_CONFIG:
                        soup = click_dynamic_crawl(row, page_number)
                    else:
                        logger.error(f"[알 수 없는 crawl_type] {site_name}: {ct}")
                        break

                    if soup:
                        success = True
                        logger.info(f"[성공] {site_name} - 페이지 {page_number}")
                    else:
                        retries += 1

                except TimeoutException:
                    retries += 1
                    logger.warning(f"[타임아웃] {site_name} - 페이지 {page_number} - 재시도 {retries}/{MAX_RETRIES}")
                    time.sleep(2)
                except Exception as e:
                    retries += 1
                    logger.error(f"[크롤링 오류] {site_name} - 페이지 {page_number}: {e}")
                    time.sleep(2)

            if not success or soup is None:
                logger.error(f"[최종 실패] {site_name} - 페이지 {page_number}")
                break

            # 데이터 파싱
            try:
                if site_name == '대전광역시고시공고':
                    table_body = soup.select(row['table_body'])[1]
                else:
                    table_body = soup.select_one(row['table_body'])

                if not table_body:
                    logger.warning(f"[테이블 없음] {site_name} - 페이지 {page_number}")
                    break

                titles = table_body.select(row['title'])
                dates = table_body.select(row['date'])

                if site_name == '충청도_서천군':
                    dates = [d for d in dates if '등록일' not in d.get_text(strip=True)]

                for title, date in zip(titles, dates):
                    clean_title = title.get_text(strip=True).replace("\r", "").replace("\n", "").replace("\t", "").strip()
                    text = date.get_text(separator=" ", strip=True)
                    extracted_date = fix_date_format(extract_date_from_text(text, site_name))

                    all_titles.append(clean_title)
                    cleaned_dates.append(extracted_date)

                    if any(kw in clean_title for kw in FILTER_KEYWORDS):
                        collected_data.append({
                            "SITE_NO": row['SITE_NO'],
                            "출처": site_name,
                            "URL": row['URL'],
                            "제목": clean_title,
                            "작성일": extracted_date
                        })

            except Exception as e:
                logger.error(f"[파싱 오류] {site_name} - 페이지 {page_number}: {e}")
                break

            # 페이지 이동 조건
            if len(set(cleaned_dates)) <= 2:
                page_number += 1
                retries = 0
                time.sleep(1)
            else:
                logger.info(f"[완료] {site_name} - 충분한 데이터 수집됨")
                break

        except Exception as e:
            logger.error(f"[전체 오류] {site_name} - 페이지 {page_number}: {e}")
            break

    # 로그 데이터
    log = {
        'SITE_NAME': site_name,
        'URL': row['URL'],
        'len_tbody': len(all_titles),
        'unique_date': len(set(cleaned_dates)),
        'min_date': min(cleaned_dates) if cleaned_dates else "",
        'max_date': max(cleaned_dates) if cleaned_dates else "",
    }

    return {'data': collected_data, 'log': log}


# ─────────────────────────────────────────────
# 병렬 크롤링
# ─────────────────────────────────────────────
def run_crawling_parallel(df: pd.DataFrame, max_workers: int = 5) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    병렬 크롤링 수행
    max_workers: 동시 실행 수 (Selenium 사용 시 3~5 권장)
    """
    all_data = []
    all_logs = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(crawl_site, row): idx for idx, row in df.iterrows()}

        for future in tqdm(as_completed(futures), total=len(futures), desc="크롤링 진행"):
            idx = futures[future]
            try:
                result = future.result()
                all_data.extend(result['data'])
                all_logs.append(result['log'])
                logger.info(f"[완료] 인덱스 {idx}")
            except Exception as e:
                logger.error(f"[병렬 처리 오류] 인덱스 {idx}: {e}")

    df_fin = pd.DataFrame(all_data)
    df_log = pd.DataFrame(all_logs)
    return df_fin, df_log


# ─────────────────────────────────────────────
# Google Sheets 업로드
# ─────────────────────────────────────────────
def upload_to_google_sheets(df: pd.DataFrame, sheet_name: str):
    """Google Sheets에 데이터프레임 업로드"""
    if not GOOGLE_SHEET_ID or not GOOGLE_CREDENTIALS_JSON:
        logger.warning("[Google Sheets] 환경변수 미설정 - 업로드 건너뜀")
        return

    try:
        scopes = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_file(GOOGLE_CREDENTIALS_JSON, scopes=scopes)
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(GOOGLE_SHEET_ID)

        try:
            worksheet = sh.worksheet(sheet_name)
            worksheet.clear()
        except gspread.exceptions.WorksheetNotFound:
            worksheet = sh.add_worksheet(title=sheet_name, rows="1000", cols="20")

        # NaT, NaN 처리
        df = df.fillna("").astype(str)
        worksheet.update([df.columns.tolist()] + df.values.tolist())
        logger.info(f"[Google Sheets] '{sheet_name}' 시트 업로드 완료 ({len(df)}행)")

    except Exception as e:
        logger.error(f"[Google Sheets 오류] {e}")


# ─────────────────────────────────────────────
# 메인 실행
# ─────────────────────────────────────────────
def main():
    today = datetime.today()
    crawled_time = today.strftime('%Y-%m-%d %H:%M:%S')
    today_str = today.strftime('%Y%m%d')
    three_days_ago = today - timedelta(days=DAYS_RANGE)

    logger.info(f"===== 크롤링 시작: {crawled_time} =====")

    # 엑셀 읽기
    file_path = './crawl_test.xlsx'
    df = pd.read_excel(file_path)
    logger.info(f"총 {len(df)}개 사이트 크롤링 시작")

    # 병렬 크롤링 실행
    df_fin, df_log = run_crawling_parallel(df, max_workers=5)

    # 날짜 필터링
    if not df_fin.empty:
        df_fin['작성일'] = pd.to_datetime(df_fin['작성일'], format='%Y-%m-%d', errors='coerce')
        df_filtered = df_fin[(df_fin['작성일'] >= three_days_ago) & (df_fin['작성일'] <= today)].copy()
        df_filtered['수집일'] = crawled_time
    else:
        df_filtered = pd.DataFrame()
        logger.warning("수집된 데이터가 없습니다.")

    # 엑셀 저장
    df_log.to_excel(f'./df_log_{today_str}.xlsx', index=False)
    df_filtered.to_excel(f'./df_list_{today_str}.xlsx', index=False)
    logger.info(f"[저장 완료] df_log_{today_str}.xlsx / df_list_{today_str}.xlsx")

    # Google Sheets 업로드
    upload_to_google_sheets(df_filtered, sheet_name=f"공고목록_{today_str}")
    upload_to_google_sheets(df_log, sheet_name="크롤링로그")

    logger.info(f"===== 크롤링 완료: {datetime.today().strftime('%Y-%m-%d %H:%M:%S')} =====")


if __name__ == "__main__":
    main()
