"""
토목공사 단계별 모니터링 시스템
──────────────────────────────────
나라장터 공공데이터 API를 활용하여 도로/교량 관련 입찰공고를 수집하고,
공고번호 기준으로 단계별 진행상황을 하나의 행으로 통합하여
Google Sheets에 자동 업로드합니다.

필요 환경변수:
  DATA_GO_KR_API_KEY       - 공공데이터포털 서비스키 (인코딩 or 디코딩)
  GOOGLE_SHEET_ID          - 구글 스프레드시트 ID
  GOOGLE_CREDENTIALS_JSON  - 구글 서비스 계정 JSON
"""

import argparse
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode

import gspread
import pandas as pd
import requests
from google.oauth2.service_account import Credentials

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# ─────────────────────────────────────────────
# 환경변수 / 상수
# ─────────────────────────────────────────────
KST = timezone(timedelta(hours=9))

DATA_GO_KR_API_KEY      = os.environ.get("DATA_GO_KR_API_KEY", "")
GOOGLE_SHEET_ID         = os.environ.get("GOOGLE_SHEET_ID", "")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")

# 나라장터 API 엔드포인트
BASE_URL = "https://apis.data.go.kr/1230000"

API_ENDPOINTS = {
    "입찰공고": f"{BASE_URL}/ad/BidPublicInfoService/getBidPblancListInfoServc",
    "사전규격": f"{BASE_URL}/ao/HrcspSsstndrdInfoService/getPublicPrcureThngInfoServc",
    "발주계획": f"{BASE_URL}/ao/OrderPlanSttusService/getOrderPlanSttusListServc",
    "개찰결과": f"{BASE_URL}/as/ScsbidInfoService/getOpengResultListInfoServc",
    "계약현황": f"{BASE_URL}/ao/CntrctInfoService/getCntrctInfoListServc",
}

# ── 토목/도로/교량 키워드 필터 ──
# 포함 키워드: 하나라도 매칭되면 수집 대상
INCLUDE_KEYWORDS = [
    '교량', '교', '고가차도', '육교',
    '도로', '포장', '교면', '방수포장',
    '거더', '신축이음', '교좌장치', '교량받침',
    '아스콘', '아스팔트', '오버레이', '절삭',
    '노면', '덧씌우기',
    '보수', '점검', '진단', '보강',
]

# 제외 키워드: 하나라도 매칭되면 제외
EXCLUDE_KEYWORDS = [
    '초등학교', '중학교', '고등학교', '학교', '유치원',
    '건축', '리모델링', '인테리어', '실내',
    '아파트', '주택', '공동주택',
    '설비', '소방', '전기공사', '통신공사',
    '조경', '녹지', '공원',
]

STAGE_ORDER = ['발주계획', '사전규격', '입찰공고', '개찰결과', '계약현황']

# ─────────────────────────────────────────────
# 로깅
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# 유틸
# ─────────────────────────────────────────────
def now_kst() -> datetime:
    return datetime.now(KST)


def format_date(dt_str: str) -> str:
    """API 날짜 문자열을 YYYY-MM-DD 형식으로 변환"""
    if not dt_str:
        return ""
    dt_str = str(dt_str).strip()
    for fmt in ("%Y/%m/%d %H:%M", "%Y%m%d%H%M", "%Y-%m-%d %H:%M:%S",
                "%Y%m%d", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            parsed_len = len(fmt.replace('%Y','YYYY').replace('%m','MM').replace('%d','DD')
                            .replace('%H','HH').replace('%M','mm').replace('%S','SS')
                            .replace('%',''))
            return datetime.strptime(dt_str[:parsed_len], fmt).strftime("%Y-%m-%d")
        except (ValueError, IndexError):
            continue
    digits = ''.join(c for c in dt_str if c.isdigit())
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return dt_str


def format_amount(amt) -> str:
    """금액을 억 단위로 포맷"""
    if not amt:
        return ""
    try:
        val = float(amt)
        if val >= 100_000_000:
            return f"{val / 100_000_000:.1f}억"
        elif val >= 10_000:
            return f"{val / 10_000:.0f}만"
        return str(int(val))
    except (ValueError, TypeError):
        return str(amt)


def is_target_project(name: str) -> bool:
    """공고명이 토목/도로/교량 관련인지 판단 (제외 키워드 적용)"""
    if not name:
        return False
    # 제외 키워드에 매칭되면 바로 제외
    if any(kw in name for kw in EXCLUDE_KEYWORDS):
        return False
    # 포함 키워드에 하나라도 매칭되면 대상
    return any(kw in name for kw in INCLUDE_KEYWORDS)


# ─────────────────────────────────────────────
# 나라장터 API 호출
# ─────────────────────────────────────────────
def _get_api_key_encoded() -> str:
    """API 키를 URL 인코딩된 형태로 반환"""
    if '%' in DATA_GO_KR_API_KEY:
        return DATA_GO_KR_API_KEY
    return quote(DATA_GO_KR_API_KEY, safe='')


def call_api(endpoint: str, params: dict) -> list:
    """나라장터 API 호출 후 items 리스트 반환"""
    if not DATA_GO_KR_API_KEY:
        logger.error("[API] DATA_GO_KR_API_KEY 환경변수 미설정")
        return []

    api_key = _get_api_key_encoded()
    params.setdefault("type", "json")
    params.setdefault("numOfRows", "999")
    params.setdefault("pageNo", "1")

    all_items = []
    page = 1

    while True:
        params["pageNo"] = str(page)
        query_str = urlencode(params) + f"&ServiceKey={api_key}"
        url = f"{endpoint}?{query_str}"
        for attempt in range(3):
            try:
                resp = requests.get(url, timeout=30)
                resp.raise_for_status()
                data = resp.json()
                break
            except requests.exceptions.RequestException as e:
                logger.warning(f"[API 재시도 {attempt+1}/3] {e}")
                time.sleep(2 ** attempt)
        else:
            logger.error(f"[API 실패] {endpoint}")
            return all_items

        # 에러 응답 처리
        if "nkoneps.com.response.ResponseError" in data:
            err = data["nkoneps.com.response.ResponseError"]
            logger.warning(f"[API 에러] {err.get('header',{}).get('resultMsg','')}")
            return all_items

        body = data.get("response", {}).get("body", {})
        items = body.get("items", [])

        if not items or items == "":
            break

        if isinstance(items, dict):
            items = [items]

        all_items.extend(items)

        total_count = int(body.get("totalCount", 0))
        num_of_rows = int(body.get("numOfRows", 999))
        if page * num_of_rows >= total_count:
            break
        page += 1

    logger.info(f"[API] {endpoint.split('/')[-1]}: {len(all_items)}건")
    return all_items


# ─────────────────────────────────────────────
# 단계별 데이터 수집 → dict 리스트 반환
# 각 dict는 {공고번호, 공고명, 기관, 단계별 정보} 형태
# ─────────────────────────────────────────────
def fetch_bid_announcements(bgn_dt: str, end_dt: str) -> list:
    """입찰공고 목록 조회 (용역)"""
    items = call_api(API_ENDPOINTS["입찰공고"], {
        "inqryDiv": "1", "inqryBgnDt": bgn_dt, "inqryEndDt": end_dt,
    })
    records = []
    for item in items:
        name = item.get("bidNtceNm", "")
        if not is_target_project(name):
            continue
        records.append({
            "공고번호": item.get("bidNtceNo", ""),
            "공고명": name,
            "발주기관": item.get("ntceInsttNm", ""),
            "수요기관": item.get("dminsttNm", ""),
            "업무구분": item.get("ntceDivNm", ""),
            "입찰공고일": format_date(item.get("bidNtceDt", "")),
            "입찰마감일": format_date(item.get("bidClseDt", "")),
            "추정가격": item.get("presmptPrce", ""),
            "배정예산": item.get("asignBdgtAmt", ""),
            "계약방법": item.get("cntrctMthdNm", ""),
            "입찰방식": item.get("bidMethdNm", ""),
            "낙찰방법": item.get("sucsfbidMthdNm", ""),
            "공고URL": item.get("bidNtceDtlUrl", ""),
            "담당자": item.get("ntceInsttOfclNm", ""),
            "담당자연락처": item.get("ntceInsttOfclTelNo", ""),
            "_stage": "입찰공고",
        })
    return records


def fetch_pre_standards(bgn_dt: str, end_dt: str) -> list:
    """사전규격 목록 조회"""
    items = call_api(API_ENDPOINTS["사전규격"], {
        "inqryDiv": "1", "inqryBgnDt": bgn_dt, "inqryEndDt": end_dt,
    })
    records = []
    for item in items:
        name = item.get("prdctClsfcNoNm", "") or item.get("bidNtceNm", "")
        if not is_target_project(name):
            continue
        records.append({
            "공고번호": item.get("bidNtceNo", "") or item.get("bfSpecRgstNo", ""),
            "사전규격번호": item.get("bfSpecRgstNo", ""),
            "공고명": name,
            "발주기관": item.get("ntceInsttNm", ""),
            "수요기관": item.get("dminsttNm", ""),
            "사전규격등록일": format_date(item.get("rgstDt", "")),
            "배정예산": item.get("asignBdgtAmt", ""),
            "_stage": "사전규격",
        })
    return records


def fetch_order_plans(bgn_dt: str, end_dt: str) -> list:
    """발주계획 목록 조회 (용역) - 년월 파라미터 사용"""
    order_bgn_ym = bgn_dt[:6]
    order_end_ym = end_dt[:6]
    items = call_api(API_ENDPOINTS["발주계획"], {
        "inqryDiv": "1", "orderBgnYm": order_bgn_ym, "orderEndYm": order_end_ym,
    })
    records = []
    for item in items:
        name = item.get("bizNm", "") or item.get("orderPlanNm", "")
        if not is_target_project(name):
            continue
        bid_list = item.get("bidNtceNoList", "")
        records.append({
            "공고번호": bid_list if bid_list else item.get("orderPlanUntyNo", ""),
            "발주계획번호": item.get("orderPlanUntyNo", ""),
            "공고명": name,
            "발주기관": item.get("orderInsttNm", ""),
            "발주예정월": f"{item.get('orderYear','')}-{item.get('orderMnth','').zfill(2)}",
            "발주도급금액": item.get("sumOrderAmt", ""),
            "_stage": "발주계획",
        })
    return records


def fetch_opening_results(bgn_dt: str, end_dt: str) -> list:
    """개찰결과 목록 조회 (용역)"""
    items = call_api(API_ENDPOINTS["개찰결과"], {
        "inqryDiv": "1", "inqryBgnDt": bgn_dt, "inqryEndDt": end_dt,
    })
    records = []
    for item in items:
        name = item.get("bidNtceNm", "")
        if not is_target_project(name):
            continue
        records.append({
            "공고번호": item.get("bidNtceNo", ""),
            "공고명": name,
            "발주기관": item.get("ntceInsttNm", ""),
            "개찰일시": format_date(item.get("opengDt", "")),
            "낙찰자": item.get("sucsfbiddrNm", ""),
            "낙찰금액": item.get("sucsfbidAmt", ""),
            "낙찰률": item.get("sucsfbidRate", ""),
            "참가업체수": item.get("prtcptCnum", ""),
            "_stage": "개찰결과",
        })
    return records


def fetch_contracts(bgn_dt: str, end_dt: str) -> list:
    """계약현황 목록 조회 (용역)"""
    items = call_api(API_ENDPOINTS["계약현황"], {
        "inqryDiv": "1", "inqryBgnDt": bgn_dt, "inqryEndDt": end_dt,
    })
    records = []
    for item in items:
        name = item.get("cntrctNm", "") or item.get("bidNtceNm", "")
        if not is_target_project(name):
            continue
        # 업체 목록에서 첫 번째 업체명 추출
        corp_nm = ""
        corp_list = item.get("corpList", "")
        if isinstance(corp_list, list) and corp_list:
            corp_nm = corp_list[0].get("corpNm", "") if isinstance(corp_list[0], dict) else ""
        elif isinstance(corp_list, dict):
            corp_nm = corp_list.get("corpNm", "")

        records.append({
            "공고번호": item.get("bidNtceNo", "") or item.get("ntceNo", ""),
            "확정계약번호": item.get("dcsnCntrctNo", "") or item.get("untyCntrctNo", ""),
            "공고명": name,
            "발주기관": item.get("cntrctInsttNm", ""),
            "계약업체": corp_nm,
            "총계약금액": item.get("totCntrctAmt", ""),
            "금차계약금액": item.get("thtmCntrctAmt", ""),
            "계약체결일": format_date(item.get("cntrctCnclsDate", "") or item.get("cntrctDate", "")),
            "계약기간": item.get("cntrctPrd", ""),
            "장기계속구분": item.get("lngtrmCtnuDivNm", ""),
            "공동계약여부": item.get("cmmnCntrctYn", ""),
            "_stage": "계약현황",
        })
    return records


# ─────────────────────────────────────────────
# 공고번호 기준 통합 (가로 합치기)
# ─────────────────────────────────────────────
def merge_by_bid_no(all_records: list) -> pd.DataFrame:
    """공고번호를 키로 동일 공사를 하나의 행으로 합치기"""
    projects = {}  # 공고번호 → 통합 데이터

    for rec in all_records:
        bid_no = rec.get("공고번호", "")
        if not bid_no:
            continue

        stage = rec.pop("_stage", "")

        if bid_no not in projects:
            projects[bid_no] = {
                "공고번호": bid_no,
                "공고명": rec.get("공고명", ""),
                "발주기관": rec.get("발주기관", ""),
                "수요기관": "",
                # 단계별 날짜
                "발주예정월": "",
                "사전규격등록일": "",
                "입찰공고일": "",
                "입찰마감일": "",
                "개찰일시": "",
                "계약체결일": "",
                # 금액
                "배정예산": "",
                "추정가격": "",
                "낙찰금액": "",
                "총계약금액": "",
                # 결과
                "낙찰자": "",
                "계약업체": "",
                "참가업체수": "",
                # 기타
                "계약방법": "",
                "입찰방식": "",
                "계약기간": "",
                "공고URL": "",
                "담당자": "",
                "담당자연락처": "",
                # 진행 단계 추적
                "_stages_found": set(),
            }

        proj = projects[bid_no]
        proj["_stages_found"].add(stage)

        # 공고명이 비어있으면 채우기
        if not proj["공고명"] and rec.get("공고명"):
            proj["공고명"] = rec["공고명"]
        if not proj["발주기관"] and rec.get("발주기관"):
            proj["발주기관"] = rec["발주기관"]
        if not proj["수요기관"] and rec.get("수요기관"):
            proj["수요기관"] = rec["수요기관"]

        # 단계별 정보 채우기 (비어있는 필드만)
        for key in rec:
            if key in ("공고번호", "공고명", "발주기관", "수요기관"):
                continue
            if key in proj and not proj[key] and rec[key]:
                proj[key] = rec[key]

    # 현재단계 결정 (가장 진행된 단계)
    for proj in projects.values():
        stages = proj.pop("_stages_found")
        latest_stage = ""
        for s in STAGE_ORDER:
            if s in stages:
                latest_stage = s
        proj["현재단계"] = latest_stage
        # 진행경로
        proj["진행경로"] = " → ".join(s for s in STAGE_ORDER if s in stages)

    if not projects:
        return pd.DataFrame()

    df = pd.DataFrame(projects.values())

    # 금액 표시 컬럼 추가
    for col in ["배정예산", "추정가격", "낙찰금액", "총계약금액"]:
        if col in df.columns:
            df[f"{col}_표시"] = df[col].apply(format_amount)

    # D-Day 계산 (입찰마감일 기준)
    today_date = now_kst().date()
    if "입찰마감일" in df.columns:
        def calc_dday(val):
            if not val or not isinstance(val, str) or len(val) < 10:
                return ""
            try:
                target = datetime.strptime(val[:10], "%Y-%m-%d").date()
                diff = (target - today_date).days
                if diff > 0:
                    return f"D-{diff}"
                elif diff == 0:
                    return "D-Day"
                else:
                    return f"D+{abs(diff)}"
            except ValueError:
                return ""
        df["마감D-Day"] = df["입찰마감일"].apply(calc_dday)

    # 컬럼 순서 정리
    col_order = [
        "공고번호", "공고명", "발주기관", "수요기관", "현재단계", "진행경로",
        "발주예정월", "사전규격등록일", "입찰공고일", "입찰마감일", "마감D-Day",
        "개찰일시", "계약체결일",
        "배정예산", "배정예산_표시", "추정가격", "추정가격_표시",
        "낙찰금액", "낙찰금액_표시", "총계약금액", "총계약금액_표시",
        "낙찰자", "계약업체", "참가업체수",
        "계약방법", "입찰방식", "계약기간",
        "공고URL", "담당자", "담당자연락처",
    ]
    existing_cols = [c for c in col_order if c in df.columns]
    extra_cols = [c for c in df.columns if c not in col_order]
    df = df[existing_cols + extra_cols]

    # 단계 순서 정렬 (최신 단계가 먼저)
    stage_rank = {s: i for i, s in enumerate(STAGE_ORDER)}
    df["_rank"] = df["현재단계"].map(stage_rank).fillna(99)
    df = df.sort_values(["_rank", "공고명"]).drop(columns=["_rank"])

    # 중복 제거 (같은 공고번호)
    df = df.drop_duplicates(subset=["공고번호"], keep="first")

    logger.info(f"총 {len(df)}건 통합 완료 (중복 제거 후)")
    return df


# ─────────────────────────────────────────────
# 통합 수집
# ─────────────────────────────────────────────
def collect_all(days_back: int = 30) -> pd.DataFrame:
    """모든 단계의 데이터를 수집하여 공고번호 기준으로 통합"""
    today = now_kst()
    bgn_dt = (today - timedelta(days=days_back)).strftime("%Y%m%d0000")
    end_dt = today.strftime("%Y%m%d2359")

    logger.info(f"[수집] 기간: {bgn_dt} ~ {end_dt}")

    # 각 단계별 수집 (dict 리스트)
    all_records = []
    all_records.extend(fetch_order_plans(bgn_dt, end_dt))
    all_records.extend(fetch_pre_standards(bgn_dt, end_dt))
    all_records.extend(fetch_bid_announcements(bgn_dt, end_dt))
    all_records.extend(fetch_opening_results(bgn_dt, end_dt))
    all_records.extend(fetch_contracts(bgn_dt, end_dt))

    if not all_records:
        logger.warning("[수집] 데이터 없음 (모든 API 결과 0건)")
        return pd.DataFrame()

    # 공고번호 기준 통합
    df = merge_by_bid_no(all_records)

    if not df.empty:
        df["수집일시"] = today.strftime("%Y-%m-%d %H:%M KST")

    return df


# ─────────────────────────────────────────────
# Google Sheets 업로드
# ─────────────────────────────────────────────
def get_gc():
    """Google Sheets 클라이언트 생성"""
    if not GOOGLE_CREDENTIALS_JSON:
        logger.warning("[GSheets] GOOGLE_CREDENTIALS_JSON 환경변수 미설정")
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


def upload_monitoring(gc, df: pd.DataFrame):
    """모니터링 데이터를 구글 시트에 업로드"""
    if gc is None or df.empty:
        return

    try:
        sh = gc.open_by_key(GOOGLE_SHEET_ID)

        # ── 1) 토목공사_현황 시트 ──
        try:
            ws = sh.worksheet("토목공사_현황")
        except gspread.exceptions.WorksheetNotFound:
            ws = sh.add_worksheet(title="토목공사_현황", rows="500", cols="30")

        display_cols = [c for c in df.columns if not c.startswith("_")]
        df_display = df[display_cols].fillna("").astype(str)

        ws.clear()
        ws.update([df_display.columns.tolist()] + df_display.values.tolist(),
                   value_input_option="USER_ENTERED")
        logger.info(f"[토목공사_현황] {len(df_display)}행 업로드 완료")

        # ── 2) 토목공사_대시보드 시트 ──
        try:
            ws_dash = sh.worksheet("토목공사_대시보드")
        except gspread.exceptions.WorksheetNotFound:
            ws_dash = sh.add_worksheet(title="토목공사_대시보드", rows="50", cols="10")

        stage_counts = df["현재단계"].value_counts()
        total = len(df)

        # D-Day 임박 건수
        urgent = 0
        if "마감D-Day" in df.columns:
            for val in df["마감D-Day"]:
                if isinstance(val, str) and (val == "D-Day" or
                    (val.startswith("D-") and val != "D-Day")):
                    try:
                        days = int(val.replace("D-", ""))
                        if 0 < days <= 3:
                            urgent += 1
                    except ValueError:
                        if val == "D-Day":
                            urgent += 1

        crawled_time = now_kst().strftime("%Y-%m-%d %H:%M KST")

        dashboard_data = [
            ["토목공사 모니터링 대시보드", "", f"기준: {crawled_time}"],
            [""],
            ["[요약]"],
            ["구분", "건수"],
            ["전체 건수", total],
            ["D-Day 임박 (3일내)", urgent],
            [""],
            ["[단계별 현황]"],
            ["단계", "건수"],
        ]
        for stage in STAGE_ORDER:
            dashboard_data.append([stage, int(stage_counts.get(stage, 0))])

        dashboard_data += [
            [""],
            ["[입찰 마감 임박 (상위 10건)]"],
            ["공고명", "발주기관", "마감일", "D-Day", "추정가격"],
        ]

        bid_df = df[df["현재단계"] == "입찰공고"].head(10)
        for _, row in bid_df.iterrows():
            dashboard_data.append([
                str(row.get("공고명", "")),
                str(row.get("발주기관", "")),
                str(row.get("입찰마감일", "")),
                str(row.get("마감D-Day", "")),
                str(row.get("추정가격_표시", "")),
            ])

        ws_dash.clear()
        ws_dash.update(dashboard_data, value_input_option="USER_ENTERED")
        logger.info("[토목공사_대시보드] 업로드 완료")

    except Exception as e:
        logger.error(f"[GSheets 업로드 오류] {e}")


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="토목공사 단계별 모니터링 시스템")
    parser.add_argument("--days", type=int, default=30,
                        help="조회 기간 (기본: 30일)")
    parser.add_argument("--no-upload", action="store_true",
                        help="구글 시트 업로드 생략 (로컬 저장만)")
    parser.add_argument("--add-keywords", nargs="+",
                        help="추가 포함 키워드")
    parser.add_argument("--add-exclude", nargs="+",
                        help="추가 제외 키워드")
    args = parser.parse_args()

    logger.info(f"===== 토목공사 모니터링 시작: {now_kst():%Y-%m-%d %H:%M:%S KST} =====")

    if args.add_keywords:
        INCLUDE_KEYWORDS.extend(args.add_keywords)
        logger.info(f"[포함 키워드 추가] {args.add_keywords}")
    if args.add_exclude:
        EXCLUDE_KEYWORDS.extend(args.add_exclude)
        logger.info(f"[제외 키워드 추가] {args.add_exclude}")

    # 데이터 수집
    df = collect_all(days_back=args.days)

    if df.empty:
        logger.warning("수집된 데이터가 없습니다.")
        logger.info("===== 완료 =====")
        return

    # 로컬 Excel 저장
    time_str = now_kst().strftime("%Y%m%d_%H%M%S")
    excel_path = f"./civil_monitor_{time_str}.xlsx"
    with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="통합현황", index=False)
        for stage in STAGE_ORDER:
            stage_df = df[df["현재단계"] == stage]
            if not stage_df.empty:
                stage_df.to_excel(writer, sheet_name=stage, index=False)

    logger.info(f"[로컬 저장] {excel_path}")

    # Google Sheets 업로드
    if not args.no_upload:
        gc = get_gc()
        upload_monitoring(gc, df)
    else:
        logger.info("[업로드 생략] --no-upload 옵션")

    logger.info(f"===== 토목공사 모니터링 완료: {now_kst():%Y-%m-%d %H:%M:%S KST} =====")


if __name__ == "__main__":
    main()
