"""
발주처별 계약 단계 모니터링 v2
─────────────────────────────
나라장터 API로 토목공사/설계 공고를 발견하고,
계약과정통합공개 API로 각 사업의 실제 단계를 추적.
단계 변경 시 이벤트로 기록하여 Google Sheets에 보고.

환경변수:
  DATA_GO_KR_API_KEY       공공데이터포털 서비스키
  GOOGLE_SHEET_ID          구글 스프레드시트 ID
  GOOGLE_CREDENTIALS_JSON  구글 서비스 계정 JSON
  WATCH_CLIENTS            (선택) 관심 발주처 쉼표구분
"""

import argparse
import json
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

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
# 설정
# ─────────────────────────────────────────────
KST = timezone(timedelta(hours=9))

DATA_GO_KR_API_KEY      = os.environ.get("DATA_GO_KR_API_KEY", "")
GOOGLE_SHEET_ID         = os.environ.get("GOOGLE_SHEET_ID", "")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")
WATCH_CLIENTS           = [c.strip() for c in os.environ.get("WATCH_CLIENTS", "").split(",") if c.strip()]

BASE = "http://apis.data.go.kr/1230000"
DB   = "./monitor.db"

ENDPOINTS = {
    "입찰공고": f"{BASE}/BidPublicInfoService/getBidPblancListInfoServc",
    "사전규격": f"{BASE}/PrdctSpcfctInfoService/getPreStndrdInfoServc",
    "발주계획": f"{BASE}/ao/OrderPlanSttusService/getOrderPlanSttusListSrvce",
    "개찰결과": f"{BASE}/ScsbidInfoService/getOpengResultListInfoServc",
    "계약현황": f"{BASE}/CntrctInfoService/getCntrctInfoListServc",
    "통합조회": f"{BASE}/ao/CntrctProcssIntgOpenService/getCntrctProcssIntgOpenServc",
}

단계순서 = ["발주계획", "사전규격", "입찰공고", "개찰결과", "계약현황"]
단계순위 = {s: i for i, s in enumerate(단계순서)}

# 이벤트 타입
신규     = "신규"
단계진행 = "단계진행"
정정공고 = "정정공고"
정보갱신 = "정보갱신"
취소유찰 = "취소유찰"

# 토목이 주 관심사
토목키워드 = [
    "토목", "도로", "교량", "터널", "하천", "상하수도", "포장",
    "배수", "옹벽", "절토", "성토", "지반", "측량", "구조물",
    "암거", "관로", "우수", "하수", "오수", "댐", "제방",
    "항만", "준설", "매립", "철도", "궤도", "고가", "지하차도",
]
기타키워드 = [
    "설계", "감리", "CM", "건설사업관리", "타당성",
    "건축", "조경", "전기", "통신", "기계", "소방",
]
전체키워드 = 토목키워드 + 기타키워드

# ─────────────────────────────────────────────
# 로깅
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# 유틸
# ─────────────────────────────────────────────
def 지금() -> datetime:
    return datetime.now(KST)


def 날짜변환(raw) -> str:
    if not raw:
        return ""
    digits = "".join(c for c in str(raw) if c.isdigit())
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return str(raw).strip()


def 금액표시(val) -> str:
    try:
        n = float(val)
        if n >= 1_0000_0000:
            return f"{n / 1_0000_0000:.1f}억"
        if n >= 1_0000:
            return f"{n / 1_0000:.0f}만"
        return f"{int(n):,}"
    except (ValueError, TypeError):
        return ""


def 디데이(date_str: str) -> str:
    if not date_str or len(date_str) < 10:
        return ""
    try:
        d = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
        diff = (d - 지금().date()).days
        if diff > 0:  return f"D-{diff}"
        if diff == 0: return "D-Day"
        return f"D+{abs(diff)}"
    except ValueError:
        return ""


def 분류(name: str) -> str:
    if not name:
        return ""
    if any(k in name for k in 토목키워드):
        return "토목"
    if any(k in name for k in 기타키워드):
        return "기타"
    return ""


def 키워드매칭(name: str) -> bool:
    return any(k in name for k in 전체키워드)


# ─────────────────────────────────────────────
# SQLite
# ─────────────────────────────────────────────
def db_초기화():
    con = sqlite3.connect(DB)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS projects (
            bid_no         TEXT PRIMARY KEY,
            bid_ord        TEXT DEFAULT '00',
            order_plan_no  TEXT,
            spec_no        TEXT,
            cntrct_no      TEXT,

            사업명         TEXT,
            발주처         TEXT,
            공고기관       TEXT,
            분류           TEXT,

            현재단계       TEXT,
            활성여부       INTEGER DEFAULT 1,

            공고일         TEXT,
            마감일         TEXT,
            개찰일         TEXT,
            계약일         TEXT,
            납기일         TEXT,

            배정예산       REAL,
            추정가격       REAL,
            계약금액       REAL,

            낙찰업체       TEXT,
            상세URL        TEXT,
            원본JSON       TEXT,

            최초수집       TEXT,
            최종갱신       TEXT,
            최종동기화     TEXT
        );

        CREATE TABLE IF NOT EXISTS events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            bid_no      TEXT,
            유형        TEXT,
            이전단계    TEXT,
            현재단계    TEXT,
            설명        TEXT,
            감지일시    TEXT,
            FOREIGN KEY (bid_no) REFERENCES projects(bid_no)
        );

        CREATE TABLE IF NOT EXISTS sync_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            시작        TEXT,
            종료        TEXT,
            신규        INTEGER,
            단계변경    INTEGER,
            갱신        INTEGER,
            오류        INTEGER
        );
    """)
    con.commit()
    con.close()


@contextmanager
def db연결():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


# ─────────────────────────────────────────────
# 나라장터 API 공통
# ─────────────────────────────────────────────
def _서비스키() -> str:
    """이중 인코딩 방지: 사용자가 인코딩 키를 넣어도 디코딩해서 사용.
    requests의 params=가 다시 인코딩하므로 디코딩 키 형태가 필요."""
    key = DATA_GO_KR_API_KEY.strip()
    # %로 인코딩된 흔적이 있으면 한번 디코딩
    if "%" in key:
        return unquote(key)
    return key


def api호출(endpoint: str, params: dict) -> list:
    if not DATA_GO_KR_API_KEY:
        log.error("[API] DATA_GO_KR_API_KEY 미설정")
        return []

    params["ServiceKey"] = _서비스키()
    params.setdefault("type", "json")
    params.setdefault("numOfRows", "999")

    전체 = []
    page = 1

    while True:
        params["pageNo"] = str(page)
        data = None
        for retry in range(3):
            try:
                r = requests.get(endpoint, params=params, timeout=30)
                r.raise_for_status()
                data = r.json()
                break
            except Exception as e:
                log.warning(f"[API 재시도 {retry+1}/3] {e}")
                time.sleep(2 ** retry)

        if data is None:
            log.error(f"[API 실패] {endpoint.split('/')[-1]}")
            break

        body  = data.get("response", {}).get("body", {})
        items = body.get("items", [])
        if not items or items == "":
            break
        if isinstance(items, dict):
            items = [items]
        전체.extend(items)

        total = int(body.get("totalCount", 0))
        rows  = int(body.get("numOfRows", 999))
        if page * rows >= total:
            break
        page += 1

    return 전체


# ─────────────────────────────────────────────
# Phase 1: 발견 — 신규 후보 수집
# ─────────────────────────────────────────────
def 발견(시작일: str, 종료일: str) -> list[dict]:
    """입찰공고 + 사전규격 + 발주계획에서 키워드 매칭되는 신규 후보 수집"""
    후보 = []

    # 입찰공고
    for item in api호출(ENDPOINTS["입찰공고"], {
        "inqryDiv": "1", "inqryBgnDt": 시작일, "inqryEndDt": 종료일,
    }):
        name = item.get("bidNtceNm", "")
        if not 키워드매칭(name):
            continue
        후보.append({
            "bid_no":     item.get("bidNtceNo", ""),
            "bid_ord":    item.get("bidNtceOrd", "00"),
            "사업명":     name,
            "발주처":     item.get("dminsttNm", ""),
            "공고기관":   item.get("ntceInsttNm", ""),
            "분류":       분류(name),
            "발견단계":   "입찰공고",
            "공고일":     날짜변환(item.get("bidNtceDt", "")),
            "마감일":     날짜변환(item.get("bidClseDt", "")),
            "추정가격":   item.get("presmptPrce"),
            "배정예산":   item.get("asignBdgtAmt"),
            "상세URL":    item.get("bidNtceDtlUrl", ""),
            "공고종류":   item.get("ntceKindNm", ""),
            "원본":       item,
        })
    log.info(f"[발견-입찰공고] {len(후보)}건")

    n = len(후보)
    # 사전규격
    for item in api호출(ENDPOINTS["사전규격"], {
        "inqryDiv": "1", "inqryBgnDt": 시작일, "inqryEndDt": 종료일,
    }):
        name = item.get("prdctClsfcNoNm", "") or item.get("bidNtceNm", "")
        if not 키워드매칭(name):
            continue
        후보.append({
            "bid_no":     item.get("bfSpecRgstNo", ""),
            "bid_ord":    "00",
            "사업명":     name,
            "발주처":     item.get("dminsttNm", ""),
            "공고기관":   item.get("ntceInsttNm", ""),
            "분류":       분류(name),
            "발견단계":   "사전규격",
            "공고일":     날짜변환(item.get("rgstDt", "")),
            "마감일":     "",
            "추정가격":   None,
            "배정예산":   item.get("asignBdgtAmt"),
            "상세URL":    "",
            "공고종류":   "",
            "원본":       item,
        })
    log.info(f"[발견-사전규격] {len(후보) - n}건")

    n = len(후보)
    # 발주계획
    for item in api호출(ENDPOINTS["발주계획"], {
        "inqryDiv": "1", "inqryBgnDt": 시작일, "inqryEndDt": 종료일,
    }):
        name = item.get("orderPlanNm", "") or item.get("bidNtceNm", "")
        if not 키워드매칭(name):
            continue
        후보.append({
            "bid_no":     item.get("orderPlanNo", ""),
            "bid_ord":    "00",
            "사업명":     name,
            "발주처":     item.get("orderInsttNm", ""),
            "공고기관":   item.get("orderInsttNm", ""),
            "분류":       분류(name),
            "발견단계":   "발주계획",
            "공고일":     날짜변환(item.get("orderPlanDt", "")),
            "마감일":     "",
            "추정가격":   None,
            "배정예산":   item.get("asignBdgtAmt"),
            "상세URL":    "",
            "공고종류":   "",
            "원본":       item,
        })
    log.info(f"[발견-발주계획] {len(후보) - n}건")

    # 관심 발주처 필터
    if WATCH_CLIENTS:
        before = len(후보)
        후보 = [h for h in 후보
                if any(c in (h["발주처"] + h["공고기관"]) for c in WATCH_CLIENTS)]
        log.info(f"[발주처 필터] {before} → {len(후보)}건")

    log.info(f"[발견 합계] {len(후보)}건")
    return 후보


# ─────────────────────────────────────────────
# Phase 2: 추적 — 통합 API로 실제 단계 확인
# ─────────────────────────────────────────────
def 단계판정(응답: dict) -> tuple[str, dict]:
    """통합 API 응답에서 현재 단계 도출 + 부가정보 추출.
    가장 진행된 단계를 현재 단계로 판정."""
    부가 = {}

    # 계약 정보 있으면 → 계약현황
    if 응답.get("cntrctNo") or 응답.get("cntrctCnclsDt"):
        부가["계약번호"]  = 응답.get("cntrctNo", "")
        부가["계약금액"]  = 응답.get("cntrctAmt")
        부가["낙찰업체"]  = 응답.get("cntrctCorpNm", "") or 응답.get("sucsfbiddrNm", "")
        부가["계약일"]    = 날짜변환(응답.get("cntrctCnclsDt", ""))
        부가["납기일"]    = 날짜변환(응답.get("dlvrDayNm", ""))
        return "계약현황", 부가

    # 낙찰 정보 있으면 → 개찰결과
    if 응답.get("sucsfbiddrNm") or 응답.get("opengDt"):
        부가["낙찰업체"]  = 응답.get("sucsfbiddrNm", "")
        부가["계약금액"]  = 응답.get("sucsfbidAmt")
        부가["개찰일"]    = 날짜변환(응답.get("opengDt", ""))
        return "개찰결과", 부가

    # 입찰공고
    if 응답.get("bidNtceNo") and 응답.get("bidNtceDt"):
        부가["마감일"] = 날짜변환(응답.get("bidClseDt", ""))
        return "입찰공고", 부가

    # 사전규격
    if 응답.get("bfSpecRgstNo"):
        return "사전규격", 부가

    # 발주계획
    if 응답.get("orderPlanNo"):
        return "발주계획", 부가

    return "미상", 부가


def 통합추적(bid_no: str) -> tuple[str, dict] | None:
    """계약과정통합공개 API로 사업의 현재 단계를 확인"""
    items = api호출(ENDPOINTS["통합조회"], {"bidNtceNo": bid_no})
    if not items:
        return None
    item = items[0] if isinstance(items, list) else items
    return 단계판정(item)


# ─────────────────────────────────────────────
# Phase 3: 차분 엔진 — DB 비교 + 이벤트 생성
# ─────────────────────────────────────────────
def 처리(con, 후보목록: list[dict]) -> dict:
    """후보를 DB와 비교하여 이벤트 생성. 통계 반환."""
    now = 지금().isoformat()
    stats = {"신규": 0, "단계변경": 0, "정정": 0, "갱신": 0, "취소유찰": 0, "오류": 0}

    처리된 = set()

    for h in 후보목록:
        bid_no = h["bid_no"]
        if not bid_no or bid_no in 처리된:
            continue
        처리된.add(bid_no)

        기존 = con.execute("SELECT * FROM projects WHERE bid_no=?", (bid_no,)).fetchone()

        # 통합 API로 실제 단계 확인 시도
        추적결과 = 통합추적(bid_no)
        if 추적결과:
            실제단계, 부가 = 추적결과
        else:
            실제단계 = h["발견단계"]
            부가 = {}

        # 취소/유찰 체크
        공고종류 = h.get("공고종류", "")
        취소여부 = any(w in 공고종류 for w in ["취소", "유찰", "무효", "철회"])

        if 기존:
            이전단계 = 기존["현재단계"]
            이전차수 = 기존["bid_ord"]
            새차수   = h.get("bid_ord", "00")

            if 취소여부:
                # 취소/유찰
                con.execute("UPDATE projects SET 현재단계=?, 활성여부=0, 최종갱신=? WHERE bid_no=?",
                            (f"취소유찰({공고종류})", now, bid_no))
                con.execute(
                    "INSERT INTO events (bid_no, 유형, 이전단계, 현재단계, 설명, 감지일시) VALUES (?,?,?,?,?,?)",
                    (bid_no, 취소유찰, 이전단계, "취소유찰", 공고종류, now))
                stats["취소유찰"] += 1

            elif 새차수 != 이전차수 and 새차수 > 이전차수:
                # 차수 변경 (정정공고)
                con.execute("""
                    UPDATE projects SET bid_ord=?, 현재단계=?, 마감일=COALESCE(?,마감일),
                    상세URL=COALESCE(?,상세URL), 최종갱신=?, 최종동기화=? WHERE bid_no=?
                """, (새차수, 실제단계, 부가.get("마감일") or h.get("마감일"),
                      h.get("상세URL"), now, now, bid_no))
                con.execute(
                    "INSERT INTO events (bid_no, 유형, 이전단계, 현재단계, 설명, 감지일시) VALUES (?,?,?,?,?,?)",
                    (bid_no, 정정공고, 이전단계, 실제단계, f"차수 {이전차수}→{새차수}", now))
                stats["정정"] += 1

            elif 단계순위.get(실제단계, -1) > 단계순위.get(이전단계, -1):
                # 단계 진행
                updates = {
                    "현재단계": 실제단계,
                    "낙찰업체": 부가.get("낙찰업체"),
                    "계약금액": 부가.get("계약금액"),
                    "계약일":   부가.get("계약일"),
                    "납기일":   부가.get("납기일"),
                    "개찰일":   부가.get("개찰일"),
                    "마감일":   부가.get("마감일") or h.get("마감일"),
                }
                set_clauses = []
                vals = []
                for k, v in updates.items():
                    if v:
                        set_clauses.append(f"{k}=?")
                        vals.append(v)
                set_clauses.append("최종갱신=?")
                vals.append(now)
                set_clauses.append("최종동기화=?")
                vals.append(now)
                vals.append(bid_no)
                con.execute(f"UPDATE projects SET {','.join(set_clauses)} WHERE bid_no=?", vals)
                con.execute(
                    "INSERT INTO events (bid_no, 유형, 이전단계, 현재단계, 설명, 감지일시) VALUES (?,?,?,?,?,?)",
                    (bid_no, 단계진행, 이전단계, 실제단계, f"{이전단계} → {실제단계}", now))
                stats["단계변경"] += 1

            else:
                # 메타데이터만 갱신
                con.execute("UPDATE projects SET 최종갱신=?, 최종동기화=? WHERE bid_no=?",
                            (now, now, bid_no))
                stats["갱신"] += 1

        else:
            # 신규 프로젝트
            활성 = 0 if 취소여부 else 1
            stage = f"취소유찰({공고종류})" if 취소여부 else 실제단계

            con.execute("""
                INSERT INTO projects
                (bid_no, bid_ord, 사업명, 발주처, 공고기관, 분류,
                 현재단계, 활성여부, 공고일, 마감일, 개찰일, 계약일, 납기일,
                 배정예산, 추정가격, 계약금액, 낙찰업체, 상세URL, 원본JSON,
                 최초수집, 최종갱신, 최종동기화)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                bid_no, h.get("bid_ord", "00"), h["사업명"], h["발주처"], h["공고기관"],
                h["분류"], stage, 활성,
                h.get("공고일", ""), 부가.get("마감일") or h.get("마감일", ""),
                부가.get("개찰일", ""), 부가.get("계약일", ""), 부가.get("납기일", ""),
                h.get("배정예산"), h.get("추정가격"), 부가.get("계약금액"),
                부가.get("낙찰업체", ""), h.get("상세URL", ""),
                json.dumps(h.get("원본", {}), ensure_ascii=False),
                now, now, now,
            ))

            이벤트유형 = 취소유찰 if 취소여부 else 신규
            con.execute(
                "INSERT INTO events (bid_no, 유형, 이전단계, 현재단계, 설명, 감지일시) VALUES (?,?,?,?,?,?)",
                (bid_no, 이벤트유형, "", stage, h["사업명"][:50], now))
            stats["신규" if not 취소여부 else "취소유찰"] += 1

    return stats


def 기존사업_추적(con) -> dict:
    """DB에 있는 활성 사업 중 통합 API 호출이 필요한 것들을 갱신"""
    stats = {"단계변경": 0, "갱신": 0}
    now = 지금().isoformat()

    rows = con.execute("""
        SELECT bid_no, 현재단계 FROM projects
        WHERE 활성여부=1 AND 현재단계 NOT IN ('계약현황')
    """).fetchall()

    if not rows:
        return stats

    log.info(f"[기존 추적] 활성 {len(rows)}건 통합 API 확인")

    for row in rows:
        bid_no = row["bid_no"]
        이전 = row["현재단계"]
        결과 = 통합추적(bid_no)
        if not 결과:
            continue

        실제단계, 부가 = 결과

        if 단계순위.get(실제단계, -1) > 단계순위.get(이전, -1):
            updates = {"현재단계": 실제단계, "최종갱신": now, "최종동기화": now}
            for k in ["낙찰업체", "계약금액", "계약일", "납기일", "개찰일", "마감일"]:
                if 부가.get(k):
                    updates[k] = 부가[k]

            set_parts = [f"{k}=?" for k in updates]
            con.execute(f"UPDATE projects SET {','.join(set_parts)} WHERE bid_no=?",
                        list(updates.values()) + [bid_no])
            con.execute(
                "INSERT INTO events (bid_no, 유형, 이전단계, 현재단계, 설명, 감지일시) VALUES (?,?,?,?,?,?)",
                (bid_no, 단계진행, 이전, 실제단계, f"{이전} → {실제단계}", now))
            stats["단계변경"] += 1
        else:
            con.execute("UPDATE projects SET 최종동기화=? WHERE bid_no=?", (now, bid_no))
            stats["갱신"] += 1

    return stats


# ─────────────────────────────────────────────
# DB → DataFrame
# ─────────────────────────────────────────────
def 전체사업() -> pd.DataFrame:
    with db연결() as con:
        rows = con.execute("SELECT * FROM projects ORDER BY 최종갱신 DESC").fetchall()
    return pd.DataFrame([dict(r) for r in rows]) if rows else pd.DataFrame()


def 최근이벤트(days: int = 7) -> pd.DataFrame:
    cutoff = (지금() - timedelta(days=days)).isoformat()
    with db연결() as con:
        rows = con.execute("""
            SELECT e.감지일시, e.bid_no, e.유형, e.이전단계, e.현재단계, e.설명,
                   p.사업명, p.발주처, p.분류
            FROM events e
            JOIN projects p ON e.bid_no = p.bid_no
            WHERE e.감지일시 >= ?
            ORDER BY e.감지일시 DESC
        """, (cutoff,)).fetchall()
    return pd.DataFrame([dict(r) for r in rows]) if rows else pd.DataFrame()


# ─────────────────────────────────────────────
# Google Sheets
# ─────────────────────────────────────────────
def gc연결():
    if not GOOGLE_CREDENTIALS_JSON:
        log.warning("[GSheets] GOOGLE_CREDENTIALS_JSON 미설정")
        return None
    try:
        creds = Credentials.from_service_account_info(
            json.loads(GOOGLE_CREDENTIALS_JSON),
            scopes=["https://spreadsheets.google.com/feeds",
                    "https://www.googleapis.com/auth/drive"])
        return gspread.authorize(creds)
    except Exception as e:
        log.error(f"[GSheets 오류] {e}")
        return None


def _시트(sh, title, rows=500, cols=15):
    try:
        return sh.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows=str(rows), cols=str(cols))


def 시트업로드(gc, df_all: pd.DataFrame, df_events: pd.DataFrame):
    if gc is None:
        return
    try:
        sh = gc.open_by_key(GOOGLE_SHEET_ID)
    except Exception as e:
        log.error(f"[GSheets 열기 오류] {e}")
        return

    기준 = 지금().strftime("%Y-%m-%d %H:%M KST")

    # ── 🔥 이번 주 이벤트 ──
    ws = _시트(sh, "🔥 이번주 이벤트")
    data = [["🔥 이번 주 이벤트", "", f"기준: {기준}"], [""]]

    if not df_events.empty:
        # 유형별 요약
        유형별 = df_events["유형"].value_counts()
        data += [["▶ 이벤트 요약"], ["유형", "건수"]]
        for t in [신규, 단계진행, 정정공고, 취소유찰, 정보갱신]:
            if t in 유형별:
                data.append([t, int(유형별[t])])
        data.append([""])

        # 토목 이벤트
        토목ev = df_events[df_events["분류"] == "토목"]
        if not 토목ev.empty:
            data += [["▶ 토목공사 이벤트", f"{len(토목ev)}건"],
                     ["일시", "유형", "사업명", "변경내용", "발주처"]]
            for _, r in 토목ev.iterrows():
                data.append([
                    str(r["감지일시"])[:16], r["유형"], r["사업명"],
                    r["설명"], r["발주처"],
                ])
            data.append([""])

        # 전체 이벤트
        data += [["▶ 전체 이벤트", f"{len(df_events)}건"],
                 ["일시", "유형", "사업명", "변경내용", "발주처", "분류"]]
        for _, r in df_events.iterrows():
            data.append([
                str(r["감지일시"])[:16], r["유형"], r["사업명"],
                r["설명"], r["발주처"], r.get("분류", ""),
            ])
    else:
        data.append(["이번 주 이벤트 없음"])

    ws.clear()
    ws.update(data, value_input_option="USER_ENTERED")
    log.info("[🔥 이번주 이벤트] 업로드")

    if df_all.empty:
        return

    # ── 🚧 토목공사 추적 ──
    토목 = df_all[df_all["분류"] == "토목"].copy()
    if not 토목.empty:
        ws2 = _시트(sh, "🚧 토목공사 추적")
        토목["D-Day"] = 토목["마감일"].apply(디데이)
        토목["추정가_표시"] = 토목["추정가격"].apply(금액표시)
        토목["계약금_표시"] = 토목["계약금액"].apply(금액표시)

        out = [["🚧 토목공사 추적", f"총 {len(토목)}건", f"기준: {기준}"], [""],
               ["공고번호", "사업명", "발주처", "현재단계", "공고일", "마감일",
                "D-Day", "추정가격", "계약금액", "낙찰업체", "활성"]]
        for _, r in 토목.iterrows():
            out.append([
                str(r["bid_no"]), str(r["사업명"]), str(r["발주처"]),
                str(r["현재단계"]), str(r.get("공고일", "")), str(r.get("마감일", "")),
                str(r["D-Day"]), str(r["추정가_표시"]), str(r["계약금_표시"]),
                str(r.get("낙찰업체", "")),
                "진행" if r.get("활성여부") else "종료",
            ])
        ws2.clear()
        ws2.update(out, value_input_option="USER_ENTERED")
        log.info(f"[🚧 토목공사] {len(토목)}행 업로드")

    # ── 🏢 발주처별 매트릭스 ──
    ws3 = _시트(sh, "🏢 발주처별")
    활성 = df_all[df_all["활성여부"] == 1]
    if not 활성.empty:
        pivot = 활성.groupby(["발주처", "현재단계"]).size().unstack(fill_value=0)
        for s in 단계순서:
            if s not in pivot.columns:
                pivot[s] = 0
        pivot = pivot[단계순서]
        pivot["합계"] = pivot.sum(axis=1)
        pivot = pivot.sort_values("합계", ascending=False)

        out = [["🏢 발주처별 현황", "", "", "", "", "", f"기준: {기준}"], [""],
               ["발주처"] + 단계순서 + ["합계"]]
        for client, row in pivot.head(30).iterrows():
            out.append([client] + [int(row[s]) for s in 단계순서] + [int(row["합계"])])
        ws3.clear()
        ws3.update(out, value_input_option="USER_ENTERED")
        log.info(f"[🏢 발주처별] {len(pivot)}개 기관 업로드")

    # ── 📋 전체 마스터 ──
    ws4 = _시트(sh, "📋 전체 마스터", 2000, 15)
    df_all["D-Day"] = df_all["마감일"].apply(디데이)
    df_all["추정가_표시"] = df_all["추정가격"].apply(금액표시)
    df_all["계약금_표시"] = df_all["계약금액"].apply(금액표시)

    cols = ["bid_no", "사업명", "발주처", "분류", "현재단계",
            "공고일", "마감일", "D-Day", "추정가_표시", "계약금_표시",
            "낙찰업체", "활성여부"]
    headers = ["공고번호", "사업명", "발주처", "분류", "현재단계",
               "공고일", "마감일", "D-Day", "추정가격", "계약금액",
               "낙찰업체", "활성"]
    existing = [c for c in cols if c in df_all.columns]
    out_df = df_all[existing].fillna("").astype(str)
    hmap = dict(zip(cols, headers))
    out_df.columns = [hmap.get(c, c) for c in existing]

    ws4.clear()
    ws4.update([out_df.columns.tolist()] + out_df.values.tolist(),
                value_input_option="USER_ENTERED")
    log.info(f"[📋 전체 마스터] {len(out_df)}행 업로드")

    # ── 📜 이벤트 로그 ──
    if not df_events.empty:
        ws5 = _시트(sh, "📜 이벤트 로그", 2000, 8)
        ev_out = df_events[["감지일시", "bid_no", "유형", "사업명",
                             "설명", "발주처", "분류"]].fillna("").astype(str)
        ev_out.columns = ["일시", "공고번호", "유형", "사업명",
                          "변경내용", "발주처", "분류"]
        ws5.clear()
        ws5.update([ev_out.columns.tolist()] + ev_out.values.tolist(),
                    value_input_option="USER_ENTERED")
        log.info(f"[📜 이벤트 로그] {len(ev_out)}행 업로드")


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="발주처별 계약 단계 모니터링 v2")
    parser.add_argument("--days", type=int, default=7, help="수집 기간 (기본 7일)")
    parser.add_argument("--no-upload", action="store_true", help="구글 시트 업로드 생략")
    parser.add_argument("--client", type=str, help="특정 발주처만 필터")
    parser.add_argument("--stats", action="store_true", help="DB 통계")
    args = parser.parse_args()

    db_초기화()

    if args.stats:
        with db연결() as con:
            total = con.execute("SELECT COUNT(*) c FROM projects").fetchone()["c"]
            active = con.execute("SELECT COUNT(*) c FROM projects WHERE 활성여부=1").fetchone()["c"]
            by_stage = con.execute(
                "SELECT 현재단계, COUNT(*) c FROM projects GROUP BY 현재단계 ORDER BY c DESC"
            ).fetchall()
            by_type = con.execute(
                "SELECT 분류, COUNT(*) c FROM projects GROUP BY 분류 ORDER BY c DESC"
            ).fetchall()
            recent = con.execute(
                "SELECT 유형, COUNT(*) c FROM events WHERE 감지일시 >= ? GROUP BY 유형",
                ((지금() - timedelta(days=7)).isoformat(),)
            ).fetchall()
        print(f"\n전체: {total}건 (활성 {active}건)")
        print("\n단계별:")
        for r in by_stage: print(f"  {r['현재단계']}: {r['c']}건")
        print("\n분류별:")
        for r in by_type: print(f"  {r['분류'] or '미분류'}: {r['c']}건")
        if recent:
            print("\n최근 7일 이벤트:")
            for r in recent: print(f"  {r['유형']}: {r['c']}건")
        return

    log.info(f"===== 모니터링 시작: {지금():%Y-%m-%d %H:%M:%S KST} =====")

    if args.client:
        global WATCH_CLIENTS
        WATCH_CLIENTS = [args.client]

    시작 = 지금()

    # Phase 1: 발견
    bgn = (시작 - timedelta(days=args.days)).strftime("%Y%m%d0000")
    end = 시작.strftime("%Y%m%d2359")
    후보 = 발견(bgn, end)

    # Phase 2+3: 추적 + 차분
    with db연결() as con:
        stats1 = 처리(con, 후보)
        stats2 = 기존사업_추적(con)

    총신규   = stats1["신규"]
    총변경   = stats1["단계변경"] + stats2["단계변경"]
    총정정   = stats1["정정"]
    총취소   = stats1["취소유찰"]

    log.info(f"[결과] 신규 {총신규} / 단계변경 {총변경} / 정정 {총정정} / 취소유찰 {총취소}")

    # sync_log 기록
    with db연결() as con:
        con.execute(
            "INSERT INTO sync_log (시작, 종료, 신규, 단계변경, 갱신, 오류) VALUES (?,?,?,?,?,?)",
            (시작.isoformat(), 지금().isoformat(), 총신규, 총변경,
             stats1["갱신"] + stats2["갱신"], stats1["오류"]))

    # 데이터 조회
    df_all = 전체사업()
    df_events = 최근이벤트(days=args.days)

    # 로컬 저장
    if not df_all.empty:
        path = f"./monitor_{시작:%Y%m%d}.xlsx"
        with pd.ExcelWriter(path, engine="openpyxl") as w:
            df_all.to_excel(w, sheet_name="전체", index=False)
            if not df_events.empty:
                df_events.to_excel(w, sheet_name="이벤트", index=False)
            토목 = df_all[df_all["분류"] == "토목"]
            if not 토목.empty:
                토목.to_excel(w, sheet_name="토목", index=False)
        log.info(f"[로컬 저장] {path}")

    # Google Sheets
    if not args.no_upload:
        gc = gc연결()
        시트업로드(gc, df_all, df_events)
    else:
        log.info("[업로드 생략]")

    log.info(f"===== 완료: {지금():%Y-%m-%d %H:%M:%S KST} =====")


if __name__ == "__main__":
    main()
