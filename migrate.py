"""
crawl_test.xlsx 구조 마이그레이션 스크립트
────────────────────────────────────────────
구 컬럼 → 신 컬럼 변환 후 crawl_test.xlsx 덮어쓰기
신 시트 '클릭설정' 추가 (CLICK_CONFIG 코드 하드코딩 → 데이터)

실행: python migrate.py
"""

import json
import math
import pandas as pd

# ── CLICK_CONFIG (기존 코드에서 추출) ─────────────────────────────────────────
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

# 대전광역시고시공고, 충청도_서천군 특수 설정 (기존 _EXTRA_CONFIGS)
_EXTRA_CONFIGS = {
    '대전광역시고시공고': {'tbody_index': 1},
    '충청도_서천군':     {'date_exclude_text': '등록일'},
}

# 사이트별 d2 click_button (크롤러코드에서 row['click_button']으로 사용했던 값)
# → 마이그레이션 시 extra_config.click_button 으로 이동
# (실제 값은 Excel에서 읽어옴)


def is_empty(v) -> bool:
    return v is None or v == "" or (isinstance(v, float) and math.isnan(v))


def get_fetch_type(row) -> str:
    ct = str(row.get('crawl_type', '') or '')
    return 'http' if ct in ('s', 'p') else 'selenium'


def get_page_type(row) -> str:
    ct2 = row.get('ct2')
    div = str(row.get('div', '') or '')
    if not is_empty(ct2) and str(ct2).startswith('cd'):
        return 'click'
    if div == 'V2':
        return 'url_param'
    return 'none'


def get_page_config(row) -> str:
    ct2 = row.get('ct2')
    if not is_empty(ct2):
        return str(ct2)
    return ''


def get_extra_config(row) -> str:
    extra = {}
    ct   = str(row.get('crawl_type', '') or '')
    name = str(row.get('SITE_NAME', '') or '')

    # POST 방식
    if ct == 'p':
        extra['method'] = 'post'

    # d1: pre_click (ofr_pageSize 드롭다운 선택)
    if ct == 'd1':
        extra['pre_click'] = {
            'id':    'ofr_pageSize',
            'xpath': '//*[@id="ofr_pageSize"]/option[1]',
        }

    # d2: 페이지 로드 후 content 전환용 클릭 버튼
    if ct == 'd2':
        cb = row.get('click_button')
        if not is_empty(cb):
            extra['click_button'] = str(cb)

    # 기존 _EXTRA_CONFIGS 인라인
    extra.update(_EXTRA_CONFIGS.get(name, {}))

    return json.dumps(extra, ensure_ascii=False) if extra else ''


def get_status(row) -> str:
    div = str(row.get('div', '') or '')
    if 'IP차단' in div or 'blocked' in div.lower():
        return 'blocked'
    if div.lower() == 'fail' or div == 'fail':
        return 'fail'
    return 'active'


def main():
    print("=== crawl_test.xlsx 마이그레이션 시작 ===")
    df = pd.read_excel('crawl_test.xlsx')
    print(f"기존 컬럼: {list(df.columns)}")
    print(f"기존 행 수: {len(df)}")

    # ── 신 사이트목록 시트 ──────────────────────────────────────────────────
    new_df = pd.DataFrame()
    new_df['SITE_NO']      = df['SITE_NO']
    new_df['SITE_NAME']    = df['SITE_NAME']
    new_df['URL']          = df['URL']
    new_df['fetch_type']   = df.apply(get_fetch_type,   axis=1)
    new_df['page_type']    = df.apply(get_page_type,    axis=1)
    new_df['page_config']  = df.apply(get_page_config,  axis=1)
    new_df['table_body']   = df['table_body']
    new_df['title']        = df['title']
    new_df['date']         = df['date']
    new_df['extra_config'] = df.apply(get_extra_config, axis=1)
    new_df['status']       = df.apply(get_status,       axis=1)

    # ── 신 클릭설정 시트 ────────────────────────────────────────────────────
    click_rows = [
        {'key': k, 'selector_type': v[0], 'selector': v[1], 'wait_sec': v[2]}
        for k, v in CLICK_CONFIG.items()
    ]
    df_click = pd.DataFrame(click_rows)

    # ── 저장 ────────────────────────────────────────────────────────────────
    with pd.ExcelWriter('crawl_test.xlsx', engine='openpyxl') as writer:
        new_df.to_excel(writer,    sheet_name='사이트목록', index=False)
        df_click.to_excel(writer,  sheet_name='클릭설정',   index=False)

    # 결과 확인
    print("\n=== 마이그레이션 완료 ===")
    print(f"신 컬럼: {list(new_df.columns)}")
    print(f"행 수: {len(new_df)}")
    print("\n[fetch_type 분포]")
    print(new_df['fetch_type'].value_counts().to_string())
    print("\n[page_type 분포]")
    print(new_df['page_type'].value_counts().to_string())
    print("\n[status 분포]")
    print(new_df['status'].value_counts().to_string())
    print("\n[extra_config 있는 사이트 수]")
    print((new_df['extra_config'] != '').sum())
    print("\n[클릭설정 시트]")
    print(f"  {len(df_click)}개 패턴")


if __name__ == '__main__':
    main()
