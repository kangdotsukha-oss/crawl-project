"""
토목공사 단계별 모니터링 시스템
──────────────────────────────────
나라장터 공공데이터 API를 활용하여 도로/교량 관련 입찰공고를 수집하고,
공고번호 기준으로 단계별 진행상황을 하나의 행으로 통합하여
Google Sheets에 자동 업로드합니다.

영업 지원 기능:
  - 설계용역 vs 시공공사 자동 분류
  - 발주처 유형 분류 (도로공사/국토부/지자체/공사공단)
  - 설계사 추적 → 공사 시기 예측
  - 금액 원 단위 정규화

필요 환경변수:
  DATA_GO_KR_API_KEY       - 공공데이터포털 서비스키
  GOOGLE_SHEET_ID          - 구글 스프레드시트 ID
  GOOGLE_CREDENTIALS_JSON  - 구글 서비스 계정 JSON
"""

import argparse
import json
import logging
import os
import re
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

BASE_URL = "https://apis.data.go.kr/1230000"

API_ENDPOINTS = {
    "입찰공고": f"{BASE_URL}/ad/BidPublicInfoService/getBidPblancListInfoServc",
    "사전규격": f"{BASE_URL}/ao/HrcspSsstndrdInfoService/getPublicPrcureThngInfoServc",
    "발주계획": f"{BASE_URL}/ao/OrderPlanSttusService/getOrderPlanSttusListServc",
    "개찰결과": f"{BASE_URL}/as/ScsbidInfoService/getOpengResultListInfoServc",
    "계약현황": f"{BASE_URL}/ao/CntrctInfoService/getCntrctInfoListServc",
}

# ── 키워드 필터 ──
INCLUDE_KEYWORDS = [
    '교량', '교', '고가차도', '육교',
    '도로', '포장', '교면', '방수포장',
    '거더', '신축이음', '교좌장치', '교량받침',
    '아스콘', '아스팔트', '오버레이', '절삭',
    '노면', '덧씌우기',
    '보수', '점검', '진단', '보강',
]

EXCLUDE_KEYWORDS = [
    '초등학교', '중학교', '고등학교', '학교', '유치원', '대학교',
    '건축', '리모델링', '인테리어', '실내',
    '아파트', '주택', '공동주택',
    '설비', '소방', '전기공사', '통신공사',
    '조경', '녹지', '공원',
    '설계', '점검',
]

# 제외 발주기관 키워드: 하나라도 매칭되면 제외
EXCLUDE_AGENCIES = [
    '교육청', '교육부', '교육지원청', '교육문화',
    '국방부', '국방시설본부', '군사', '사단', '여단', '연대',
    '해군', '공군', '육군', '해병대', '국군', '방위사업청',
]

# ── 업무구분 분류 키워드 ──
DESIGN_KEYWORDS = ['설계', '감리', 'CM', '건설사업관리', '타당성', '기본계획', '실시설계', '기본설계']
CONSTRUCTION_KEYWORDS = ['공사', '시공', '보수공사', '개량공사', '포장공사', '유지보수']

# ── 발주처 분류 ──
AGENCY_TYPES = {
    "한국도로공사": ["한국도로공사", "도로공사", "고속도로"],
    "국토부": ["국토교통부", "국토부", "익산지방국토관리청", "원주지방국토관리청",
               "대전지방국토관리청", "부산지방국토관리청", "서울지방국토관리청",
               "지방국토관리청", "국토관리청", "새만금개발청"],
    "공사공단": ["한국수자원공사", "수자원공사", "LH", "한국토지주택공사",
                "한국철도공사", "철도공사", "한국도로공사", "도로공사",
                "한국환경공단", "한국수력원자력", "한국전력공사",
                "한국농어촌공사", "농어촌공사"],
    "지자체": [],  # 나머지는 모두 지자체로 분류
}

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


def to_won(amt) -> int:
    """금액을 원 단위 정수로 변환"""
    if not amt:
        return 0
    try:
        return int(float(amt))
    except (ValueError, TypeError):
        return 0


def is_target_project(name: str, agency: str = "") -> bool:
    """공고명이 토목/도로/교량 관련인지 판단 (제외 기관 포함)"""
    if not name:
        return False
    # 제외 기관
    if agency and any(kw in agency for kw in EXCLUDE_AGENCIES):
        return False
    # 제외 키워드
    if any(kw in name for kw in EXCLUDE_KEYWORDS):
        return False
    # 포함 키워드
    return any(kw in name for kw in INCLUDE_KEYWORDS)


def classify_work_type(name: str, bsns_div: str = "") -> str:
    """업무구분 분류: 설계용역 / 시공공사 / 기타"""
    if not name:
        return "기타"
    # API 업무구분 필드 우선
    if bsns_div:
        if "용역" in bsns_div or "기술" in bsns_div:
            if any(kw in name for kw in DESIGN_KEYWORDS):
                return "설계용역"
        if "공사" in bsns_div:
            return "시공공사"
    # 키워드 기반 2차 분류
    if any(kw in name for kw in DESIGN_KEYWORDS):
        return "설계용역"
    if any(kw in name for kw in CONSTRUCTION_KEYWORDS):
        return "시공공사"
    return "기타"


def classify_agency(agency_name: str) -> tuple:
    """발주처 분류 → (유형, 세부지역)"""
    if not agency_name:
        return ("기타", "")

    # 1차: 기관유형 분류
    agency_type = "지자체"  # 기본값
    for atype, keywords in AGENCY_TYPES.items():
        if atype == "지자체":
            continue
        if any(kw in agency_name for kw in keywords):
            agency_type = atype
            break

    # 2차: 세부지역 추출
    region = ""
    # 광역시/도
    metros = ["서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종",
              "경기", "강원", "충북", "충남", "전북", "전남", "경북", "경남", "제주"]
    for m in metros:
        if m in agency_name:
            region = m
            break

    # 시/군/구 추출
    match = re.search(r'(\w{1,4}(?:시|군|구))', agency_name)
    if match:
        detail = match.group(1)
        if detail not in ["특별시", "광역시", "특별자치시", "특별자치도"]:
            if region:
                region = f"{region} {detail}"
            else:
                region = detail

    return (agency_type, region)


def estimate_construction_date(contract_date: str, contract_period: str) -> str:
    """설계 계약일 + 계약기간으로 공사 입찰 예상 시기 추정"""
    if not contract_date or not contract_period:
        return ""
    try:
        start = datetime.strptime(contract_date[:10], "%Y-%m-%d")
        # 계약기간에서 일수 추출 (예: "365일", "12개월", "2025-12-31")
        days = 0
        if "일" in contract_period:
            nums = re.findall(r'\d+', contract_period)
            if nums:
                days = int(nums[0])
        elif "개월" in contract_period or "월" in contract_period:
            nums = re.findall(r'\d+', contract_period)
            if nums:
                days = int(nums[0]) * 30
        elif "-" in contract_period:
            # 종료일 직접 지정 형태
            try:
                end = datetime.strptime(contract_period[:10], "%Y-%m-%d")
                days = (end - start).days
            except ValueError:
                pass

        if days > 0:
            est_date = start + timedelta(days=days)
            return est_date.strftime("%Y-%m")
    except (ValueError, IndexError):
        pass
    return ""


# ─────────────────────────────────────────────
# 나라장터 API 호출
# ─────────────────────────────────────────────
def _get_api_key_encoded() -> str:
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
# 단계별 데이터 수집
# ─────────────────────────────────────────────
def fetch_bid_announcements(bgn_dt: str, end_dt: str) -> list:
    items = call_api(API_ENDPOINTS["입찰공고"], {
        "inqryDiv": "1", "inqryBgnDt": bgn_dt, "inqryEndDt": end_dt,
    })
    records = []
    for item in items:
        name = item.get("bidNtceNm", "")
        agency = item.get("ntceInsttNm", "")
        if not is_target_project(name, agency):
            continue
        records.append({
            "공고번호": item.get("bidNtceNo", ""),
            "공고명": name,
            "발주기관": agency,
            "수요기관": item.get("dminsttNm", ""),
            "_bsns_div": item.get("ntceDivNm", ""),
            "입찰공고일": format_date(item.get("bidNtceDt", "")),
            "입찰마감일": format_date(item.get("bidClseDt", "")),
            "추정가격": to_won(item.get("presmptPrce", "")),
            "배정예산": to_won(item.get("asignBdgtAmt", "")),
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
    items = call_api(API_ENDPOINTS["사전규격"], {
        "inqryDiv": "1", "inqryBgnDt": bgn_dt, "inqryEndDt": end_dt,
    })
    records = []
    for item in items:
        name = item.get("prdctClsfcNoNm", "") or item.get("bidNtceNm", "")
        agency = item.get("orderInsttNm", "") or item.get("rlDminsttNm", "")
        if not is_target_project(name, agency):
            continue
        bid_no = item.get("bidNtceNoList", "") or item.get("bfSpecRgstNo", "")
        records.append({
            "공고번호": bid_no,
            "사전규격번호": item.get("bfSpecRgstNo", ""),
            "공고명": name,
            "발주기관": agency,
            "수요기관": item.get("rlDminsttNm", ""),
            "_bsns_div": item.get("bsnsDivNm", ""),
            "사전규격등록일": format_date(item.get("rgstDt", "")),
            "배정예산": to_won(item.get("asignBdgtAmt", "")),
            "_stage": "사전규격",
        })
    return records


def fetch_order_plans(bgn_dt: str, end_dt: str) -> list:
    order_bgn_ym = bgn_dt[:6]
    order_end_ym = end_dt[:6]
    items = call_api(API_ENDPOINTS["발주계획"], {
        "inqryDiv": "1", "orderBgnYm": order_bgn_ym, "orderEndYm": order_end_ym,
    })
    records = []
    for item in items:
        name = item.get("bizNm", "") or item.get("orderPlanNm", "")
        agency = item.get("orderInsttNm", "")
        if not is_target_project(name, agency):
            continue
        bid_list = item.get("bidNtceNoList", "")
        records.append({
            "공고번호": bid_list if bid_list else item.get("orderPlanUntyNo", ""),
            "발주계획번호": item.get("orderPlanUntyNo", ""),
            "공고명": name,
            "발주기관": item.get("orderInsttNm", ""),
            "_bsns_div": item.get("bsnsDivNm", ""),
            "발주예정월": f"{item.get('orderYear','')}-{item.get('orderMnth','').zfill(2)}",
            "배정예산": to_won(item.get("sumOrderAmt", "")),
            "_stage": "발주계획",
        })
    return records


def fetch_opening_results(bgn_dt: str, end_dt: str) -> list:
    items = call_api(API_ENDPOINTS["개찰결과"], {
        "inqryDiv": "1", "inqryBgnDt": bgn_dt, "inqryEndDt": end_dt,
    })
    records = []
    for item in items:
        name = item.get("bidNtceNm", "")
        agency = item.get("ntceInsttNm", "")
        if not is_target_project(name, agency):
            continue
        # 낙찰자 추출: sucsfbiddrNm 또는 opengCorpInfo에서 파싱
        winner = item.get("sucsfbiddrNm", "")
        if not winner:
            corp_info = item.get("opengCorpInfo", "")
            if corp_info and "^" in corp_info:
                winner = corp_info.split("^")[0]  # 첫 번째 필드가 회사명

        records.append({
            "공고번호": item.get("bidNtceNo", ""),
            "공고명": name,
            "발주기관": item.get("ntceInsttNm", ""),
            "_bsns_div": "",
            "개찰일시": format_date(item.get("opengDt", "")),
            "낙찰자": winner,
            "낙찰금액": to_won(item.get("sucsfbidAmt", "")),
            "낙찰률": item.get("sucsfbidRate", ""),
            "참가업체수": item.get("prtcptCnum", ""),
            "_stage": "개찰결과",
        })
    return records


def fetch_contracts(bgn_dt: str, end_dt: str) -> list:
    items = call_api(API_ENDPOINTS["계약현황"], {
        "inqryDiv": "1", "inqryBgnDt": bgn_dt, "inqryEndDt": end_dt,
    })
    records = []
    for item in items:
        name = item.get("cntrctNm", "") or item.get("bidNtceNm", "")
        agency = item.get("cntrctInsttNm", "")
        if not is_target_project(name, agency):
            continue
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
            "_bsns_div": item.get("bsnsDivNm", ""),
            "계약업체": corp_nm,
            "총계약금액": to_won(item.get("totCntrctAmt", "")),
            "금차계약금액": to_won(item.get("thtmCntrctAmt", "")),
            "계약체결일": format_date(item.get("cntrctCnclsDate", "") or item.get("cntrctDate", "")),
            "계약기간": item.get("cntrctPrd", ""),
            "장기계속구분": item.get("lngtrmCtnuDivNm", ""),
            "공동계약여부": item.get("cmmnCntrctYn", ""),
            "_stage": "계약현황",
        })
    return records


# ─────────────────────────────────────────────
# 공고번호 기준 통합 + 영업 컬럼 추가
# ─────────────────────────────────────────────
def merge_by_bid_no(all_records: list) -> pd.DataFrame:
    """공고번호를 키로 동일 공사를 하나의 행으로 합치기 + 영업 컬럼"""
    projects = {}

    for rec in all_records:
        bid_no = rec.get("공고번호", "")
        if not bid_no:
            continue

        stage = rec.pop("_stage", "")
        bsns_div = rec.pop("_bsns_div", "")

        if bid_no not in projects:
            projects[bid_no] = {
                "공고번호": bid_no,
                "공고명": rec.get("공고명", ""),
                "발주기관": rec.get("발주기관", ""),
                "수요기관": "",
                "_bsns_div_raw": bsns_div,
                # 단계별 날짜
                "발주예정월": "", "사전규격등록일": "", "입찰공고일": "",
                "입찰마감일": "", "개찰일시": "", "계약체결일": "",
                # 금액 (원 단위)
                "배정예산": 0, "추정가격": 0, "낙찰금액": 0,
                "총계약금액": 0, "금차계약금액": 0,
                # 결과
                "낙찰자": "", "계약업체": "", "낙찰률": "", "참가업체수": "",
                # 기타
                "계약방법": "", "입찰방식": "", "낙찰방법": "",
                "계약기간": "", "장기계속구분": "",
                "공고URL": "", "담당자": "", "담당자연락처": "",
                "_stages_found": set(),
            }

        proj = projects[bid_no]
        proj["_stages_found"].add(stage)

        if not proj["_bsns_div_raw"] and bsns_div:
            proj["_bsns_div_raw"] = bsns_div

        # 빈 필드 채우기
        if not proj["공고명"] and rec.get("공고명"):
            proj["공고명"] = rec["공고명"]
        if not proj["발주기관"] and rec.get("발주기관"):
            proj["발주기관"] = rec["발주기관"]
        if not proj["수요기관"] and rec.get("수요기관"):
            proj["수요기관"] = rec["수요기관"]

        for key in rec:
            if key in ("공고번호", "공고명", "발주기관", "수요기관"):
                continue
            if key in proj:
                # 숫자 필드: 0이면 채우기
                if isinstance(proj[key], int) and proj[key] == 0 and rec[key]:
                    proj[key] = rec[key]
                # 문자열 필드: 비어있으면 채우기
                elif isinstance(proj[key], str) and not proj[key] and rec[key]:
                    proj[key] = rec[key]

    # ── 영업 컬럼 계산 ──
    for proj in projects.values():
        stages = proj.pop("_stages_found")
        bsns_div = proj.pop("_bsns_div_raw", "")

        # 현재단계 / 진행경로
        latest_stage = ""
        for s in STAGE_ORDER:
            if s in stages:
                latest_stage = s
        proj["현재단계"] = latest_stage
        proj["진행경로"] = " → ".join(s for s in STAGE_ORDER if s in stages)

        # 업무구분 (설계용역 / 시공공사)
        proj["업무구분"] = classify_work_type(proj["공고명"], bsns_div)

        # 발주처유형 / 세부지역
        agency_type, region = classify_agency(proj["발주기관"])
        proj["발주처유형"] = agency_type
        proj["세부지역"] = region

        # 대표금액 (가장 신뢰할 수 있는 금액)
        proj["대표금액"] = (proj["총계약금액"] or proj["낙찰금액"]
                          or proj["추정가격"] or proj["배정예산"] or 0)

        # 설계사 추적 (설계용역인 경우)
        if proj["업무구분"] == "설계용역":
            proj["설계사"] = proj["낙찰자"] or proj["계약업체"]
            proj["설계계약일"] = proj["계약체결일"]
            proj["공사예상시기"] = estimate_construction_date(
                proj["계약체결일"], proj["계약기간"])
        else:
            proj["설계사"] = ""
            proj["설계계약일"] = ""
            proj["공사예상시기"] = ""

    if not projects:
        return pd.DataFrame()

    df = pd.DataFrame(projects.values())

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

    # 금액 0 → 빈값 처리 (표시용)
    for col in ["배정예산", "추정가격", "낙찰금액", "총계약금액", "금차계약금액", "대표금액"]:
        if col in df.columns:
            df[col] = df[col].apply(lambda x: x if x and x != 0 else "")

    # 컬럼 순서
    col_order = [
        "공고번호", "공고명", "업무구분", "발주처유형", "세부지역",
        "발주기관", "수요기관", "현재단계", "진행경로",
        "발주예정월", "사전규격등록일", "입찰공고일", "입찰마감일", "마감D-Day",
        "개찰일시", "계약체결일",
        "대표금액", "배정예산", "추정가격", "낙찰금액", "총계약금액",
        "낙찰자", "계약업체", "낙찰률", "참가업체수",
        "설계사", "설계계약일", "공사예상시기",
        "계약방법", "입찰방식", "낙찰방법", "계약기간", "장기계속구분",
        "공고URL", "담당자", "담당자연락처",
    ]
    existing_cols = [c for c in col_order if c in df.columns]
    extra_cols = [c for c in df.columns if c not in col_order and not c.startswith("_")]
    df = df[existing_cols + extra_cols]

    # 정렬: 단계 → 금액 내림차순
    stage_rank = {s: i for i, s in enumerate(STAGE_ORDER)}
    df["_rank"] = df["현재단계"].map(stage_rank).fillna(99)
    df = df.sort_values(["_rank", "공고명"]).drop(columns=["_rank"])

    # 중복 제거
    df = df.drop_duplicates(subset=["공고번호"], keep="first")

    logger.info(f"총 {len(df)}건 통합 완료 (중복 제거 후)")
    return df


# ─────────────────────────────────────────────
# 통합 수집
# ─────────────────────────────────────────────
def collect_all(days_back: int = 30) -> pd.DataFrame:
    today = now_kst()
    bgn_dt = (today - timedelta(days=days_back)).strftime("%Y%m%d0000")
    end_dt = today.strftime("%Y%m%d2359")

    logger.info(f"[수집] 기간: {bgn_dt} ~ {end_dt}")

    all_records = []
    all_records.extend(fetch_order_plans(bgn_dt, end_dt))
    all_records.extend(fetch_pre_standards(bgn_dt, end_dt))
    all_records.extend(fetch_bid_announcements(bgn_dt, end_dt))
    all_records.extend(fetch_opening_results(bgn_dt, end_dt))
    all_records.extend(fetch_contracts(bgn_dt, end_dt))

    if not all_records:
        logger.warning("[수집] 데이터 없음")
        return pd.DataFrame()

    df = merge_by_bid_no(all_records)

    if not df.empty:
        df["수집일시"] = today.strftime("%Y-%m-%d %H:%M KST")

    return df


# ─────────────────────────────────────────────
# Google Sheets 업로드
# ─────────────────────────────────────────────
def get_gc():
    if not GOOGLE_CREDENTIALS_JSON:
        logger.warning("[GSheets] GOOGLE_CREDENTIALS_JSON 미설정")
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
    if gc is None or df.empty:
        return

    try:
        sh = gc.open_by_key(GOOGLE_SHEET_ID)

        # ── 1) 전체현황 시트 ──
        ws = _get_or_create_sheet(sh, "토목공사_현황", rows=5000, cols=40)
        display_cols = [c for c in df.columns if not c.startswith("_")]
        df_display = df[display_cols].fillna("").astype(str)
        ws.clear()
        ws.update([df_display.columns.tolist()] + df_display.values.tolist(),
                   value_input_option="USER_ENTERED")
        logger.info(f"[토목공사_현황] {len(df_display)}행 업로드")

        # ── 2) 영업용 대시보드 ──
        ws_dash = _get_or_create_sheet(sh, "영업_대시보드", rows=100, cols=10)
        dashboard = _build_dashboard(df)
        ws_dash.clear()
        ws_dash.update(dashboard, value_input_option="USER_ENTERED")
        logger.info("[영업_대시보드] 업로드")

        # ── 3) 신규 사전규격 (최근 7일) ──
        ws_pre = _get_or_create_sheet(sh, "신규_사전규격", rows=500, cols=20)
        pre_df = df[df["현재단계"] == "사전규격"].head(100)
        if not pre_df.empty:
            pre_cols = ["공고명", "발주처유형", "세부지역", "발주기관",
                       "사전규격등록일", "배정예산", "업무구분"]
            pre_show = pre_df[[c for c in pre_cols if c in pre_df.columns]].fillna("").astype(str)
            ws_pre.clear()
            ws_pre.update([pre_show.columns.tolist()] + pre_show.values.tolist(),
                         value_input_option="USER_ENTERED")
            logger.info(f"[신규_사전규격] {len(pre_show)}행 업로드")

        # ── 4) 설계용역 낙찰 (설계사 추적) ──
        ws_design = _get_or_create_sheet(sh, "설계용역_낙찰", rows=500, cols=20)
        design_df = df[(df["업무구분"] == "설계용역") &
                       (df["현재단계"].isin(["개찰결과", "계약현황"]))].head(100)
        if not design_df.empty:
            d_cols = ["공고명", "발주처유형", "세부지역", "발주기관",
                     "설계사", "설계계약일", "계약기간", "공사예상시기",
                     "총계약금액", "현재단계"]
            d_show = design_df[[c for c in d_cols if c in design_df.columns]].fillna("").astype(str)
            ws_design.clear()
            ws_design.update([d_show.columns.tolist()] + d_show.values.tolist(),
                           value_input_option="USER_ENTERED")
            logger.info(f"[설계용역_낙찰] {len(d_show)}행 업로드")

    except Exception as e:
        logger.error(f"[GSheets 업로드 오류] {e}")


def _get_or_create_sheet(sh, name, rows=500, cols=30):
    try:
        return sh.worksheet(name)
    except gspread.exceptions.WorksheetNotFound:
        return sh.add_worksheet(title=name, rows=str(rows), cols=str(cols))


def _build_dashboard(df: pd.DataFrame) -> list:
    """영업용 대시보드 데이터 구성"""
    crawled_time = now_kst().strftime("%Y-%m-%d %H:%M KST")
    total = len(df)

    # 업무구분별 건수
    work_counts = df["업무구분"].value_counts() if "업무구분" in df.columns else {}

    # 발주처유형별 건수
    agency_counts = df["발주처유형"].value_counts() if "발주처유형" in df.columns else {}

    # D-Day 임박
    urgent = 0
    if "마감D-Day" in df.columns:
        for val in df["마감D-Day"]:
            if isinstance(val, str):
                if val == "D-Day":
                    urgent += 1
                elif val.startswith("D-") and val != "D-Day":
                    try:
                        if 0 < int(val.replace("D-", "")) <= 3:
                            urgent += 1
                    except ValueError:
                        pass

    # 대형 공고 (10억+)
    big_count = 0
    if "대표금액" in df.columns:
        for val in df["대표금액"]:
            try:
                if float(val) >= 1_000_000_000:
                    big_count += 1
            except (ValueError, TypeError):
                pass

    dashboard = [
        ["토목공사 영업 대시보드", "", f"기준: {crawled_time}"],
        [""],
        ["[전체 요약]"],
        ["구분", "건수"],
        ["전체", total],
        ["설계용역", int(work_counts.get("설계용역", 0))],
        ["시공공사", int(work_counts.get("시공공사", 0))],
        ["마감임박(3일내)", urgent],
        ["대형공고(10억+)", big_count],
        [""],
        ["[발주처 유형별]"],
        ["유형", "건수"],
    ]
    for atype in ["한국도로공사", "국토부", "지자체", "공사공단", "기타"]:
        dashboard.append([atype, int(agency_counts.get(atype, 0))])

    dashboard += [
        [""],
        ["[단계별 현황]"],
        ["단계", "건수"],
    ]
    stage_counts = df["현재단계"].value_counts()
    for stage in STAGE_ORDER:
        dashboard.append([stage, int(stage_counts.get(stage, 0))])

    # 마감 임박 입찰 top 10
    dashboard += [[""], ["[마감 임박 입찰 TOP 10]"],
                  ["공고명", "발주처유형", "마감일", "D-Day", "추정가격"]]
    bid_df = df[df["현재단계"] == "입찰공고"].head(10)
    for _, row in bid_df.iterrows():
        dashboard.append([
            str(row.get("공고명", ""))[:40],
            str(row.get("발주처유형", "")),
            str(row.get("입찰마감일", "")),
            str(row.get("마감D-Day", "")),
            str(row.get("추정가격", "")),
        ])

    # 설계용역 → 공사 예측 top 10
    design_df = df[(df["업무구분"] == "설계용역") & (df["설계사"] != "")].head(10)
    if not design_df.empty:
        dashboard += [[""], ["[설계 완료 → 공사 예측]"],
                      ["공고명", "설계사", "설계계약일", "공사예상시기"]]
        for _, row in design_df.iterrows():
            dashboard.append([
                str(row.get("공고명", ""))[:40],
                str(row.get("설계사", "")),
                str(row.get("설계계약일", "")),
                str(row.get("공사예상시기", "")),
            ])

    return dashboard


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="토목공사 단계별 모니터링 시스템")
    parser.add_argument("--days", type=int, default=30, help="조회 기간 (기본: 30일)")
    parser.add_argument("--no-upload", action="store_true", help="구글 시트 업로드 생략")
    parser.add_argument("--add-keywords", nargs="+", help="추가 포함 키워드")
    parser.add_argument("--add-exclude", nargs="+", help="추가 제외 키워드")
    args = parser.parse_args()

    logger.info(f"===== 토목공사 모니터링 시작: {now_kst():%Y-%m-%d %H:%M:%S KST} =====")

    if args.add_keywords:
        INCLUDE_KEYWORDS.extend(args.add_keywords)
    if args.add_exclude:
        EXCLUDE_KEYWORDS.extend(args.add_exclude)

    df = collect_all(days_back=args.days)

    if df.empty:
        logger.warning("수집된 데이터가 없습니다.")
        logger.info("===== 완료 =====")
        return

    # 로컬 Excel 저장
    time_str = now_kst().strftime("%Y%m%d_%H%M%S")
    excel_path = f"./civil_monitor_{time_str}.xlsx"
    with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="전체현황", index=False)

        # 신규 사전규격
        pre = df[df["현재단계"] == "사전규격"]
        if not pre.empty:
            pre.to_excel(writer, sheet_name="신규_사전규격", index=False)

        # 마감 임박
        if "마감D-Day" in df.columns:
            urgent = df[df["마감D-Day"].str.match(r'^D-[1-3]$|^D-Day$', na=False)]
            if not urgent.empty:
                urgent.to_excel(writer, sheet_name="마감임박_입찰", index=False)

        # 설계용역 낙찰
        design = df[(df["업무구분"] == "설계용역") &
                    (df["현재단계"].isin(["개찰결과", "계약현황"]))]
        if not design.empty:
            design.to_excel(writer, sheet_name="설계용역_낙찰", index=False)

        # 발주처유형별 요약
        if "발주처유형" in df.columns and "대표금액" in df.columns:
            summary_data = []
            for atype in df["발주처유형"].unique():
                adf = df[df["발주처유형"] == atype]
                summary_data.append({
                    "발주처유형": atype,
                    "건수": len(adf),
                    "설계용역": len(adf[adf["업무구분"] == "설계용역"]),
                    "시공공사": len(adf[adf["업무구분"] == "시공공사"]),
                })
            if summary_data:
                pd.DataFrame(summary_data).to_excel(
                    writer, sheet_name="발주처별_요약", index=False)

    logger.info(f"[로컬 저장] {excel_path}")

    if not args.no_upload:
        gc = get_gc()
        upload_monitoring(gc, df)
    else:
        logger.info("[업로드 생략] --no-upload 옵션")

    logger.info(f"===== 토목공사 모니터링 완료: {now_kst():%Y-%m-%d %H:%M:%S KST} =====")


if __name__ == "__main__":
    main()
