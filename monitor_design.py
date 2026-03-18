"""
설계용역 단계별 모니터링 시스템
──────────────────────────────────
나라장터 공공데이터 API를 활용하여 설계용역 입찰공고를 수집하고,
단계별 진행상황(발주계획→사전규격→입찰공고→개찰결과→계약현황)을 추적하여
Google Sheets에 자동 업로드합니다.

필요 환경변수:
  DATA_GO_KR_API_KEY       - 공공데이터포털 서비스키 (인코딩)
  GOOGLE_SHEET_ID          - 구글 스프레드시트 ID (기존 crawl.py와 공유)
  GOOGLE_CREDENTIALS_JSON  - 구글 서비스 계정 JSON (기존 crawl.py와 공유)
"""

import argparse
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import gspread
import pandas as pd
import requests
from google.oauth2.service_account import Credentials
from urllib.parse import unquote, urlencode

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
    # 입찰공고정보서비스 - 용역 입찰공고 목록
    "입찰공고": f"{BASE_URL}/ad/BidPublicInfoService/getBidPblancListInfoServc",
    # 사전규격정보서비스 (조달물자사전규격정보서비스)
    "사전규격": f"{BASE_URL}/ao/HrcspSsstndrdInfoService/getPublicPrcureThngInfoServc",
    # 발주계획현황서비스 - 용역 발주계획 목록
    "발주계획": f"{BASE_URL}/ao/OrderPlanSttusService/getOrderPlanSttusListServc",
    # 낙찰정보서비스 - 용역 낙찰 목록
    "개찰결과": f"{BASE_URL}/as/ScsbidInfoService/getOpengResultListInfoServc",
    # 계약정보서비스 - 용역 계약 목록
    "계약현황": f"{BASE_URL}/ao/CntrctInfoService/getCntrctInfoListServc",
    # 계약과정통합공개서비스 - 단계별 진행과정 추적 (공고번호 기반)
    "진행과정": f"{BASE_URL}/ao/CntrctProcssIntgOpenService/getCntrctProcssIntgOpenServc",
}

# 설계용역 관련 키워드 필터
DESIGN_KEYWORDS = [
    '설계', '기본설계', '실시설계', '기본및실시설계', '타당성',
    '건축설계', '도시설계', '구조설계', '토목설계', '조경설계',
    '감리', '설계감리', 'CM', '건설사업관리',
]

# 각 단계의 구글 시트 컬럼 매핑
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
    # 2025/03/18 10:00 형태
    for fmt in ("%Y/%m/%d %H:%M", "%Y%m%d%H%M", "%Y-%m-%d %H:%M:%S",
                "%Y%m%d", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(dt_str[:len(fmt.replace('%', '').replace('Y', 'YYYY').replace('m', 'MM').replace('d', 'DD').replace('H', 'HH').replace('M', 'mm').replace('S', 'SS'))], fmt).strftime("%Y-%m-%d")
        except (ValueError, IndexError):
            continue
    # 간단한 숫자 파싱 시도
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


def is_design_service(name: str) -> bool:
    """공고명이 설계용역 관련인지 판단"""
    if not name:
        return False
    return any(kw in name for kw in DESIGN_KEYWORDS)


# ─────────────────────────────────────────────
# 나라장터 API 호출
# ─────────────────────────────────────────────
def call_api(endpoint: str, params: dict) -> list:
    """나라장터 API 호출 후 items 리스트 반환"""
    if not DATA_GO_KR_API_KEY:
        logger.error("[API] DATA_GO_KR_API_KEY 환경변수 미설정")
        return []

    # data.go.kr API 키는 Encoding 버전(URL인코딩됨)을 URL에 직접 삽입해야 함
    # .env에 Decoding 키가 들어있으면 quote()로 인코딩, 이미 인코딩되어 있으면 그대로 사용
    from urllib.parse import quote
    if '%' in DATA_GO_KR_API_KEY:
        api_key = DATA_GO_KR_API_KEY  # 이미 Encoding 키
    else:
        api_key = quote(DATA_GO_KR_API_KEY, safe='')  # Decoding 키 → Encoding 키로 변환
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

        body = data.get("response", {}).get("body", {})
        items = body.get("items", [])

        # items가 빈 문자열이거나 없는 경우
        if not items or items == "":
            break

        # 단일 아이템인 경우 리스트로 변환
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
# 단계별 데이터 수집
# ─────────────────────────────────────────────
def fetch_bid_announcements(bgn_dt: str, end_dt: str) -> pd.DataFrame:
    """입찰공고 목록 조회 (용역)"""
    items = call_api(API_ENDPOINTS["입찰공고"], {
        "inqryDiv": "1",
        "inqryBgnDt": bgn_dt,
        "inqryEndDt": end_dt,
    })
    if not items:
        return pd.DataFrame()

    records = []
    for item in items:
        name = item.get("bidNtceNm", "")
        if not is_design_service(name):
            continue
        records.append({
            "공고번호": item.get("bidNtceNo", ""),
            "공고차수": item.get("bidNtceOrd", ""),
            "공고명": name,
            "공고기관": item.get("ntceInsttNm", ""),
            "수요기관": item.get("dminsttNm", ""),
            "공고일": format_date(item.get("bidNtceDt", "")),
            "마감일": format_date(item.get("bidClseDt", "")),
            "추정가격": item.get("presmptPrce", ""),
            "추정가격_표시": format_amount(item.get("presmptPrce", "")),
            "배정예산": item.get("asignBdgtAmt", ""),
            "계약방법": item.get("cntrctMthdNm", ""),
            "입찰방식": item.get("bidMethdNm", ""),
            "낙찰방법": item.get("sucsfbidMthdNm", ""),
            "공고종류": item.get("ntceKindNm", ""),
            "상세URL": item.get("bidNtceDtlUrl", ""),
            "담당자": item.get("ntceInsttOfclNm", ""),
            "담당자연락처": item.get("ntceInsttOfclTelNo", ""),
            "현재단계": "입찰공고",
        })
    return pd.DataFrame(records)


def fetch_pre_standards(bgn_dt: str, end_dt: str) -> pd.DataFrame:
    """사전규격 목록 조회 (용역)"""
    items = call_api(API_ENDPOINTS["사전규격"], {
        "inqryDiv": "1",
        "inqryBgnDt": bgn_dt,
        "inqryEndDt": end_dt,
    })
    if not items:
        return pd.DataFrame()

    records = []
    for item in items:
        name = item.get("prdctClsfcNoNm", "") or item.get("bidNtceNm", "")
        if not is_design_service(name):
            continue
        records.append({
            "사전규격번호": item.get("bfSpecRgstNo", ""),
            "공고명": name,
            "공고기관": item.get("ntceInsttNm", ""),
            "수요기관": item.get("dminsttNm", ""),
            "등록일": format_date(item.get("rgstDt", "")),
            "배정예산": item.get("asignBdgtAmt", ""),
            "배정예산_표시": format_amount(item.get("asignBdgtAmt", "")),
            "현재단계": "사전규격",
        })
    return pd.DataFrame(records)


def fetch_order_plans(bgn_dt: str, end_dt: str) -> pd.DataFrame:
    """발주계획 목록 조회 (용역) - 발주계획 API는 년월 파라미터 사용"""
    # bgn_dt: 202603010000 → orderBgnYm: 202603
    order_bgn_ym = bgn_dt[:6]
    order_end_ym = end_dt[:6]
    items = call_api(API_ENDPOINTS["발주계획"], {
        "inqryDiv": "1",
        "orderBgnYm": order_bgn_ym,
        "orderEndYm": order_end_ym,
    })
    if not items:
        return pd.DataFrame()

    records = []
    for item in items:
        name = item.get("bizNm", "") or item.get("orderPlanNm", "")
        if not is_design_service(name):
            continue
        records.append({
            "발주계획번호": item.get("orderPlanUntyNo", ""),
            "공고명": name,
            "발주기관": item.get("orderInsttNm", ""),
            "배정예산": item.get("sumOrderAmt", ""),
            "배정예산_표시": format_amount(item.get("sumOrderAmt", "")),
            "발주예정월": f"{item.get('orderYear','')}-{item.get('orderMnth','').zfill(2)}",
            "현재단계": "발주계획",
        })
    return pd.DataFrame(records)


def fetch_opening_results(bgn_dt: str, end_dt: str) -> pd.DataFrame:
    """개찰결과 목록 조회 (용역)"""
    items = call_api(API_ENDPOINTS["개찰결과"], {
        "inqryDiv": "1",
        "inqryBgnDt": bgn_dt,
        "inqryEndDt": end_dt,
    })
    if not items:
        return pd.DataFrame()

    records = []
    for item in items:
        name = item.get("bidNtceNm", "")
        if not is_design_service(name):
            continue
        records.append({
            "공고번호": item.get("bidNtceNo", ""),
            "공고명": name,
            "공고기관": item.get("ntceInsttNm", ""),
            "낙찰자": item.get("sucsfbiddrNm", ""),
            "낙찰금액": item.get("sucsfbidAmt", ""),
            "낙찰금액_표시": format_amount(item.get("sucsfbidAmt", "")),
            "개찰일": format_date(item.get("opengDt", "")),
            "현재단계": "개찰결과",
        })
    return pd.DataFrame(records)


def fetch_contracts(bgn_dt: str, end_dt: str) -> pd.DataFrame:
    """계약현황 목록 조회 (용역)"""
    items = call_api(API_ENDPOINTS["계약현황"], {
        "inqryDiv": "1",
        "inqryBgnDt": bgn_dt,
        "inqryEndDt": end_dt,
    })
    if not items:
        return pd.DataFrame()

    records = []
    for item in items:
        name = item.get("cntrctNm", "") or item.get("bidNtceNm", "")
        if not is_design_service(name):
            continue
        records.append({
            "공고번호": item.get("bidNtceNo", ""),
            "계약번호": item.get("cntrctNo", ""),
            "공고명": name,
            "공고기관": item.get("ntceInsttNm", ""),
            "수요기관": item.get("dminsttNm", ""),
            "계약업체": item.get("cntrctCorpNm", ""),
            "계약금액": item.get("cntrctAmt", ""),
            "계약금액_표시": format_amount(item.get("cntrctAmt", "")),
            "계약일": format_date(item.get("cntrctCnclsDt", "")),
            "납기일": format_date(item.get("dlvrDayNm", "")),
            "현재단계": "계약현황",
        })
    return pd.DataFrame(records)


def fetch_process_tracking(bid_ntce_no: str) -> dict:
    """특정 공고번호의 계약 진행과정 조회 (계약과정통합공개서비스)
    → 사전규격→입찰공고→낙찰→계약 전 과정을 한 번에 추적"""
    items = call_api(API_ENDPOINTS["진행과정"], {
        "bidNtceNo": bid_ntce_no,
    })
    if not items:
        return {}

    # 진행과정 정보를 단계별로 정리
    item = items[0] if isinstance(items, list) else items
    stages_found = []
    if item.get("bfSpecRgstNo"):
        stages_found.append("사전규격")
    if item.get("bidNtceNo"):
        stages_found.append("입찰공고")
    if item.get("sucsfbiddrNm") or item.get("opengDt"):
        stages_found.append("개찰결과")
    if item.get("cntrctNo") or item.get("cntrctCnclsDt"):
        stages_found.append("계약현황")

    return {
        "진행단계목록": " → ".join(stages_found) if stages_found else "",
        "최종단계": stages_found[-1] if stages_found else "",
        "낙찰자": item.get("sucsfbiddrNm", ""),
        "계약번호": item.get("cntrctNo", ""),
        "계약금액": item.get("cntrctAmt", ""),
    }


# ─────────────────────────────────────────────
# 통합 수집 + D-Day 계산
# ─────────────────────────────────────────────
def collect_all(days_back: int = 30) -> pd.DataFrame:
    """모든 단계의 설계용역 데이터를 수집하여 통합 DataFrame 반환"""
    today = now_kst()
    bgn_dt = (today - timedelta(days=days_back)).strftime("%Y%m%d0000")
    end_dt = today.strftime("%Y%m%d2359")

    logger.info(f"[수집] 기간: {bgn_dt} ~ {end_dt}")

    frames = {
        "발주계획": fetch_order_plans(bgn_dt, end_dt),
        "사전규격": fetch_pre_standards(bgn_dt, end_dt),
        "입찰공고": fetch_bid_announcements(bgn_dt, end_dt),
        "개찰결과": fetch_opening_results(bgn_dt, end_dt),
        "계약현황": fetch_contracts(bgn_dt, end_dt),
    }

    # 단계별 건수 로깅
    for stage, df in frames.items():
        logger.info(f"  {stage}: {len(df)}건")

    # 통합
    non_empty = [df for df in frames.values() if not df.empty]
    if not non_empty:
        logger.warning("[수집] 설계용역 데이터 없음 (모든 API 결과 0건)")
        return pd.DataFrame()

    all_data = pd.concat(non_empty, ignore_index=True)

    if all_data.empty:
        logger.warning("[수집] 설계용역 데이터 없음")
        return pd.DataFrame()

    # D-Day 계산
    today_date = today.date()
    for col in ["마감일", "납기일"]:
        if col in all_data.columns:
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
            all_data[f"{col}_D-Day"] = all_data[col].apply(calc_dday)

    # 수집일시 추가
    all_data["수집일시"] = today.strftime("%Y-%m-%d %H:%M KST")

    # 단계 순서 정렬
    stage_rank = {s: i for i, s in enumerate(STAGE_ORDER)}
    all_data["_stage_rank"] = all_data["현재단계"].map(stage_rank).fillna(99)
    all_data = all_data.sort_values(["_stage_rank", "공고명"]).drop(columns=["_stage_rank"])

    return all_data


# ─────────────────────────────────────────────
# Google Sheets 업로드
# ─────────────────────────────────────────────
def get_gc():
    """Google Sheets 클라이언트 생성 (기존 crawl.py와 동일한 방식)"""
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
    """설계용역 모니터링 데이터를 구글 시트에 업로드"""
    if gc is None or df.empty:
        return

    try:
        sh = gc.open_by_key(GOOGLE_SHEET_ID)

        # ── 1) 설계용역_현황 시트 ──
        try:
            ws = sh.worksheet("설계용역_현황")
        except gspread.exceptions.WorksheetNotFound:
            ws = sh.add_worksheet(title="설계용역_현황", rows="500", cols="25")

        # 표시용 컬럼만 선택 (내부 컬럼 제외)
        display_cols = [c for c in df.columns if not c.startswith("_")]
        df_display = df[display_cols].fillna("").astype(str)

        ws.clear()
        ws.update([df_display.columns.tolist()] + df_display.values.tolist(),
                   value_input_option="USER_ENTERED")
        logger.info(f"[설계용역_현황] {len(df_display)}행 업로드 완료")

        # ── 2) 설계용역_대시보드 시트 ──
        try:
            ws_dash = sh.worksheet("설계용역_대시보드")
        except gspread.exceptions.WorksheetNotFound:
            ws_dash = sh.add_worksheet(title="설계용역_대시보드", rows="50", cols="10")

        # 단계별 집계
        stage_counts = df["현재단계"].value_counts()
        total = len(df)

        # D-Day 임박 건수 (3일 이내)
        urgent = 0
        for col in ["마감일_D-Day", "납기일_D-Day"]:
            if col in df.columns:
                for val in df[col]:
                    if isinstance(val, str) and val.startswith("D-") and val != "D-Day":
                        try:
                            days = int(val.replace("D-", ""))
                            if 0 < days <= 3:
                                urgent += 1
                        except ValueError:
                            pass
                    elif val == "D-Day":
                        urgent += 1

        # 금액 합산
        total_amt = 0
        for col in ["추정가격", "계약금액", "배정예산"]:
            if col in df.columns:
                for val in df[col]:
                    try:
                        total_amt += float(val)
                    except (ValueError, TypeError):
                        pass

        crawled_time = now_kst().strftime("%Y-%m-%d %H:%M KST")

        dashboard_data = [
            ["📊 설계용역 모니터링 대시보드", "", f"기준: {crawled_time}"],
            [""],
            ["▶ 요약"],
            ["구분", "건수"],
            ["전체 건수", total],
            ["D-Day 임박 (3일내)", urgent],
            [f"총 추정금액", format_amount(total_amt)],
            [""],
            ["▶ 단계별 현황"],
            ["단계", "건수"],
        ]
        for stage in STAGE_ORDER:
            cnt = int(stage_counts.get(stage, 0))
            dashboard_data.append([stage, cnt])

        dashboard_data += [
            [""],
            ["▶ 최근 입찰공고 (상위 10건)"],
            ["공고명", "공고기관", "마감일", "D-Day", "추정가격"],
        ]

        bid_df = df[df["현재단계"] == "입찰공고"].head(10)
        for _, row in bid_df.iterrows():
            dashboard_data.append([
                str(row.get("공고명", "")),
                str(row.get("공고기관", "")),
                str(row.get("마감일", "")),
                str(row.get("마감일_D-Day", "")),
                str(row.get("추정가격_표시", "")),
            ])

        ws_dash.clear()
        ws_dash.update(dashboard_data, value_input_option="USER_ENTERED")
        logger.info("[설계용역_대시보드] 업로드 완료")

    except Exception as e:
        logger.error(f"[GSheets 업로드 오류] {e}")


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="설계용역 단계별 모니터링 시스템")
    parser.add_argument("--days", type=int, default=30,
                        help="조회 기간 (기본: 30일)")
    parser.add_argument("--no-upload", action="store_true",
                        help="구글 시트 업로드 생략 (로컬 저장만)")
    parser.add_argument("--keywords", nargs="+",
                        help="추가 필터 키워드 (예: --keywords 도로 교량)")
    args = parser.parse_args()

    logger.info(f"===== 설계용역 모니터링 시작: {now_kst():%Y-%m-%d %H:%M:%S KST} =====")

    # 추가 키워드가 있으면 필터에 추가
    if args.keywords:
        DESIGN_KEYWORDS.extend(args.keywords)
        logger.info(f"[키워드 추가] {args.keywords}")

    # 데이터 수집
    df = collect_all(days_back=args.days)

    if df.empty:
        logger.warning("수집된 설계용역 데이터가 없습니다.")
        logger.info("===== 완료 =====")
        return

    logger.info(f"총 {len(df)}건 수집 완료")

    # 로컬 Excel 저장
    today_str = now_kst().strftime("%Y%m%d")
    time_str = now_kst().strftime("%Y%m%d_%H%M%S")
    excel_path = f"./design_monitor_{time_str}.xlsx"
    with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="설계용역_현황", index=False)

        # 단계별 시트 분리
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

    logger.info(f"===== 설계용역 모니터링 완료: {now_kst():%Y-%m-%d %H:%M:%S KST} =====")


if __name__ == "__main__":
    main()
