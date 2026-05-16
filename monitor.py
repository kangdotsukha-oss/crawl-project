"""
발주처별 계약 단계 모니터링 시스템
──────────────────────────────────
나라장터 API로 토목공사/설계용역 공고를 수집하고,
공고번호 기준으로 단계 진행(발주계획→사전규격→입찰공고→개찰→계약)을 추적하여
단계 변경 시 Google Sheets에 알림.

필요 환경변수:
  DATA_GO_KR_API_KEY       - 공공데이터포털 서비스키
  GOOGLE_SHEET_ID          - 구글 스프레드시트 ID
  GOOGLE_CREDENTIALS_JSON  - 구글 서비스 계정 JSON
  WATCH_CLIENTS            - (선택) 관심 발주처 쉼표구분
"""

import argparse
import json
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

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
WATCH_CLIENTS           = [c.strip() for c in os.environ.get("WATCH_CLIENTS", "").split(",") if c.strip()]

BASE_URL = "http://apis.data.go.kr/1230000"
DB_PATH  = "./monitor.db"

ENDPOINTS = {
    "입찰공고":  f"{BASE_URL}/BidPublicInfoService/getBidPblancListInfoServc",
    "사전규격":  f"{BASE_URL}/PrdctSpcfctInfoService/getPreStndrdInfoServc",
    "발주계획":  f"{BASE_URL}/ao/OrderPlanSttusService/getOrderPlanSttusListSrvce",
    "개찰결과":  f"{BASE_URL}/ScsbidInfoService/getOpengResultListInfoServc",
    "계약현황":  f"{BASE_URL}/CntrctInfoService/getCntrctInfoListServc",
    "진행과정":  f"{BASE_URL}/ao/CntrctProcssIntgOpenService/getCntrctProcssIntgOpenServc",
}

STAGE_ORDER = ["발주계획", "사전규격", "입찰공고", "개찰결과", "계약현황"]
STAGE_RANK  = {s: i for i, s in enumerate(STAGE_ORDER)}

# 토목공사가 주 관심사, 나머지도 수집
PRIMARY_KEYWORDS = [
    "토목", "도로", "교량", "터널", "하천", "상하수도", "포장",
    "배수", "옹벽", "절토", "성토", "기반", "지반", "측량",
    "구조물", "암거", "관로", "우수", "하수", "오수",
]
SECONDARY_KEYWORDS = [
    "설계", "감리", "CM", "건설사업관리", "타당성",
    "건축", "조경", "전기", "통신", "기계", "소방",
]
ALL_KEYWORDS = PRIMARY_KEYWORDS + SECONDARY_KEYWORDS

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


def fmt_date(raw: str) -> str:
    if not raw:
        return ""
    raw = str(raw).strip()
    digits = "".join(c for c in raw if c.isdigit())
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return raw


def fmt_amount(val) -> str:
    try:
        n = float(val)
        if n >= 1_0000_0000:
            return f"{n / 1_0000_0000:.1f}억"
        if n >= 1_0000:
            return f"{n / 1_0000:.0f}만"
        return f"{int(n):,}"
    except (ValueError, TypeError):
        return ""


def calc_dday(date_str: str) -> str:
    if not date_str or len(date_str) < 10:
        return ""
    try:
        target = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
        diff = (target - now_kst().date()).days
        if diff > 0:
            return f"D-{diff}"
        if diff == 0:
            return "D-Day"
        return f"D+{abs(diff)}"
    except ValueError:
        return ""


def classify_interest(name: str) -> str:
    if not name:
        return ""
    if any(kw in name for kw in PRIMARY_KEYWORDS):
        return "토목"
    if any(kw in name for kw in SECONDARY_KEYWORDS):
        return "기타"
    return ""


def matches_keywords(name: str) -> bool:
    return any(kw in name for kw in ALL_KEYWORDS)


# ─────────────────────────────────────────────
# SQLite
# ─────────────────────────────────────────────
def init_db():
    con = sqlite3.connect(DB_PATH)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS projects (
            bid_ntce_no    TEXT PRIMARY KEY,
            project_name   TEXT,
            client_org     TEXT,
            announce_org   TEXT,
            current_stage  TEXT,
            prev_stage     TEXT,
            stage_changed  TEXT,
            announce_date  TEXT,
            deadline       TEXT,
            est_price      REAL,
            contract_amt   REAL,
            contractor     TEXT,
            detail_url     TEXT,
            interest       TEXT,
            first_seen     TEXT,
            last_updated   TEXT
        );
        CREATE TABLE IF NOT EXISTS stage_history (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            bid_ntce_no    TEXT,
            stage          TEXT,
            detected_at    TEXT,
            FOREIGN KEY (bid_ntce_no) REFERENCES projects(bid_ntce_no)
        );
        CREATE TABLE IF NOT EXISTS watch_clients (
            name TEXT PRIMARY KEY
        );
    """)
    con.commit()
    con.close()


@contextmanager
def db_conn():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def upsert_project(con, data: dict) -> bool:
    """프로젝트 upsert. 단계가 바뀌었으면 True 반환."""
    bid_no = data["bid_ntce_no"]
    now = now_kst().isoformat()

    existing = con.execute(
        "SELECT current_stage FROM projects WHERE bid_ntce_no=?", (bid_no,)
    ).fetchone()

    new_stage = data.get("current_stage", "")
    stage_changed = False

    if existing:
        old_stage = existing["current_stage"]
        new_rank = STAGE_RANK.get(new_stage, -1)
        old_rank = STAGE_RANK.get(old_stage, -1)

        # 단계가 진행된 경우만 업데이트 (역행 방지)
        if new_rank > old_rank:
            stage_changed = True
            con.execute("""
                UPDATE projects SET
                    current_stage=?, prev_stage=?, stage_changed=?,
                    deadline=COALESCE(?, deadline),
                    est_price=COALESCE(?, est_price),
                    contract_amt=COALESCE(?, contract_amt),
                    contractor=COALESCE(?, contractor),
                    detail_url=COALESCE(?, detail_url),
                    last_updated=?
                WHERE bid_ntce_no=?
            """, (
                new_stage, old_stage, now,
                data.get("deadline") or None,
                data.get("est_price") or None,
                data.get("contract_amt") or None,
                data.get("contractor") or None,
                data.get("detail_url") or None,
                now, bid_no,
            ))
            con.execute(
                "INSERT INTO stage_history (bid_ntce_no, stage, detected_at) VALUES (?,?,?)",
                (bid_no, new_stage, now),
            )
        else:
            # 단계 변경 없으면 메타 정보만 갱신
            con.execute("""
                UPDATE projects SET
                    deadline=COALESCE(?, deadline),
                    est_price=COALESCE(?, est_price),
                    contract_amt=COALESCE(?, contract_amt),
                    contractor=COALESCE(?, contractor),
                    last_updated=?
                WHERE bid_ntce_no=?
            """, (
                data.get("deadline") or None,
                data.get("est_price") or None,
                data.get("contract_amt") or None,
                data.get("contractor") or None,
                now, bid_no,
            ))
    else:
        # 신규 프로젝트
        stage_changed = True
        con.execute("""
            INSERT INTO projects
            (bid_ntce_no, project_name, client_org, announce_org,
             current_stage, prev_stage, stage_changed,
             announce_date, deadline, est_price, contract_amt,
             contractor, detail_url, interest, first_seen, last_updated)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            bid_no, data.get("project_name", ""), data.get("client_org", ""),
            data.get("announce_org", ""), new_stage, "", now,
            data.get("announce_date", ""), data.get("deadline", ""),
            data.get("est_price"), data.get("contract_amt"),
            data.get("contractor", ""), data.get("detail_url", ""),
            data.get("interest", ""), now, now,
        ))
        con.execute(
            "INSERT INTO stage_history (bid_ntce_no, stage, detected_at) VALUES (?,?,?)",
            (bid_no, new_stage, now),
        )

    return stage_changed


# ─────────────────────────────────────────────
# 나라장터 API
# ─────────────────────────────────────────────
def call_api(endpoint: str, params: dict) -> list:
    if not DATA_GO_KR_API_KEY:
        logger.error("[API] DATA_GO_KR_API_KEY 미설정")
        return []

    params["ServiceKey"] = DATA_GO_KR_API_KEY
    params.setdefault("type", "json")
    params.setdefault("numOfRows", "999")

    all_items = []
    page = 1

    while True:
        params["pageNo"] = str(page)
        for attempt in range(3):
            try:
                resp = requests.get(endpoint, params=params, timeout=30)
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception as e:
                logger.warning(f"[API 재시도 {attempt+1}/3] {e}")
                time.sleep(2 ** attempt)
        else:
            logger.error(f"[API 실패] {endpoint.split('/')[-1]}")
            return all_items

        body = data.get("response", {}).get("body", {})
        items = body.get("items", [])
        if not items or items == "":
            break
        if isinstance(items, dict):
            items = [items]
        all_items.extend(items)

        total = int(body.get("totalCount", 0))
        rows = int(body.get("numOfRows", 999))
        if page * rows >= total:
            break
        page += 1

    return all_items


# ─────────────────────────────────────────────
# 단계별 수집
# ─────────────────────────────────────────────
def collect_bid_announcements(bgn: str, end: str) -> list:
    """입찰공고 수집 → 프로젝트 데이터 리스트 반환"""
    items = call_api(ENDPOINTS["입찰공고"], {
        "inqryDiv": "1", "inqryBgnDt": bgn, "inqryEndDt": end,
    })
    results = []
    for item in items:
        name = item.get("bidNtceNm", "")
        if not matches_keywords(name):
            continue
        results.append({
            "bid_ntce_no":   item.get("bidNtceNo", ""),
            "project_name":  name,
            "client_org":    item.get("dminsttNm", ""),
            "announce_org":  item.get("ntceInsttNm", ""),
            "current_stage": "입찰공고",
            "announce_date": fmt_date(item.get("bidNtceDt", "")),
            "deadline":      fmt_date(item.get("bidClseDt", "")),
            "est_price":     item.get("presmptPrce"),
            "contract_amt":  None,
            "contractor":    "",
            "detail_url":    item.get("bidNtceDtlUrl", ""),
            "interest":      classify_interest(name),
        })
    logger.info(f"[입찰공고] {len(results)}건 (키워드 매칭)")
    return results


def collect_pre_standards(bgn: str, end: str) -> list:
    items = call_api(ENDPOINTS["사전규격"], {
        "inqryDiv": "1", "inqryBgnDt": bgn, "inqryEndDt": end,
    })
    results = []
    for item in items:
        name = item.get("prdctClsfcNoNm", "") or item.get("bidNtceNm", "")
        if not matches_keywords(name):
            continue
        results.append({
            "bid_ntce_no":   item.get("bfSpecRgstNo", ""),
            "project_name":  name,
            "client_org":    item.get("dminsttNm", ""),
            "announce_org":  item.get("ntceInsttNm", ""),
            "current_stage": "사전규격",
            "announce_date": fmt_date(item.get("rgstDt", "")),
            "deadline":      "",
            "est_price":     item.get("asignBdgtAmt"),
            "contract_amt":  None,
            "contractor":    "",
            "detail_url":    "",
            "interest":      classify_interest(name),
        })
    logger.info(f"[사전규격] {len(results)}건")
    return results


def collect_order_plans(bgn: str, end: str) -> list:
    items = call_api(ENDPOINTS["발주계획"], {
        "inqryDiv": "1", "inqryBgnDt": bgn, "inqryEndDt": end,
    })
    results = []
    for item in items:
        name = item.get("orderPlanNm", "") or item.get("bidNtceNm", "")
        if not matches_keywords(name):
            continue
        results.append({
            "bid_ntce_no":   item.get("orderPlanNo", ""),
            "project_name":  name,
            "client_org":    item.get("orderInsttNm", ""),
            "announce_org":  item.get("orderInsttNm", ""),
            "current_stage": "발주계획",
            "announce_date": fmt_date(item.get("orderPlanDt", "")),
            "deadline":      "",
            "est_price":     item.get("asignBdgtAmt"),
            "contract_amt":  None,
            "contractor":    "",
            "detail_url":    "",
            "interest":      classify_interest(name),
        })
    logger.info(f"[발주계획] {len(results)}건")
    return results


def collect_opening_results(bgn: str, end: str) -> list:
    items = call_api(ENDPOINTS["개찰결과"], {
        "inqryDiv": "1", "inqryBgnDt": bgn, "inqryEndDt": end,
    })
    results = []
    for item in items:
        name = item.get("bidNtceNm", "")
        if not matches_keywords(name):
            continue
        results.append({
            "bid_ntce_no":   item.get("bidNtceNo", ""),
            "project_name":  name,
            "client_org":    item.get("dminsttNm", "") or item.get("ntceInsttNm", ""),
            "announce_org":  item.get("ntceInsttNm", ""),
            "current_stage": "개찰결과",
            "announce_date": fmt_date(item.get("opengDt", "")),
            "deadline":      "",
            "est_price":     item.get("presmptPrce"),
            "contract_amt":  item.get("sucsfbidAmt"),
            "contractor":    item.get("sucsfbiddrNm", ""),
            "detail_url":    "",
            "interest":      classify_interest(name),
        })
    logger.info(f"[개찰결과] {len(results)}건")
    return results


def collect_contracts(bgn: str, end: str) -> list:
    items = call_api(ENDPOINTS["계약현황"], {
        "inqryDiv": "1", "inqryBgnDt": bgn, "inqryEndDt": end,
    })
    results = []
    for item in items:
        name = item.get("cntrctNm", "") or item.get("bidNtceNm", "")
        if not matches_keywords(name):
            continue
        results.append({
            "bid_ntce_no":   item.get("bidNtceNo", ""),
            "project_name":  name,
            "client_org":    item.get("dminsttNm", "") or item.get("ntceInsttNm", ""),
            "announce_org":  item.get("ntceInsttNm", ""),
            "current_stage": "계약현황",
            "announce_date": "",
            "deadline":      fmt_date(item.get("dlvrDayNm", "")),
            "est_price":     item.get("presmptPrce"),
            "contract_amt":  item.get("cntrctAmt"),
            "contractor":    item.get("cntrctCorpNm", ""),
            "detail_url":    "",
            "interest":      classify_interest(name),
        })
    logger.info(f"[계약현황] {len(results)}건")
    return results


# ─────────────────────────────────────────────
# 메인 수집 + DB 저장
# ─────────────────────────────────────────────
def collect_and_save(days_back: int = 7) -> tuple[int, int]:
    """전체 수집 → DB upsert. (신규건수, 단계변경건수) 반환"""
    today = now_kst()
    bgn = (today - timedelta(days=days_back)).strftime("%Y%m%d0000")
    end = today.strftime("%Y%m%d2359")
    logger.info(f"[수집] {bgn[:8]} ~ {end[:8]} ({days_back}일)")

    all_data = []
    all_data += collect_order_plans(bgn, end)
    all_data += collect_pre_standards(bgn, end)
    all_data += collect_bid_announcements(bgn, end)
    all_data += collect_opening_results(bgn, end)
    all_data += collect_contracts(bgn, end)

    logger.info(f"[수집 합계] {len(all_data)}건")

    # 관심 발주처 필터 (설정 시)
    if WATCH_CLIENTS:
        before = len(all_data)
        all_data = [d for d in all_data
                    if any(c in (d.get("client_org", "") + d.get("announce_org", ""))
                           for c in WATCH_CLIENTS)]
        logger.info(f"[발주처 필터] {before} → {len(all_data)}건")

    new_count = 0
    changed_count = 0

    with db_conn() as con:
        for data in all_data:
            if not data.get("bid_ntce_no"):
                continue
            changed = upsert_project(con, data)
            if changed:
                existing = con.execute(
                    "SELECT first_seen, stage_changed FROM projects WHERE bid_ntce_no=?",
                    (data["bid_ntce_no"],)
                ).fetchone()
                if existing and existing["first_seen"] == existing["stage_changed"]:
                    new_count += 1
                else:
                    changed_count += 1

    logger.info(f"[DB] 신규 {new_count}건 / 단계변경 {changed_count}건")
    return new_count, changed_count


# ─────────────────────────────────────────────
# DB → DataFrame 조회
# ─────────────────────────────────────────────
def get_all_projects() -> pd.DataFrame:
    with db_conn() as con:
        rows = con.execute("SELECT * FROM projects ORDER BY last_updated DESC").fetchall()
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([dict(r) for r in rows])


def get_stage_changes(since_days: int = 7) -> pd.DataFrame:
    cutoff = (now_kst() - timedelta(days=since_days)).isoformat()
    with db_conn() as con:
        rows = con.execute("""
            SELECT h.detected_at, h.bid_ntce_no, h.stage,
                   p.project_name, p.prev_stage, p.client_org, p.interest
            FROM stage_history h
            JOIN projects p ON h.bid_ntce_no = p.bid_ntce_no
            WHERE h.detected_at >= ?
            ORDER BY h.detected_at DESC
        """, (cutoff,)).fetchall()
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([dict(r) for r in rows])


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


def _get_or_create_ws(sh, title: str, rows: int = 500, cols: int = 20):
    try:
        return sh.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows=str(rows), cols=str(cols))


def upload_sheets(gc, df_all: pd.DataFrame, df_changes: pd.DataFrame):
    if gc is None:
        return
    try:
        sh = gc.open_by_key(GOOGLE_SHEET_ID)
    except Exception as e:
        logger.error(f"[GSheets 열기 오류] {e}")
        return

    crawled = now_kst().strftime("%Y-%m-%d %H:%M KST")

    # ── 1) 대시보드 ──
    ws = _get_or_create_ws(sh, "📊 대시보드", 80, 10)
    dash = [
        ["📊 발주처별 계약 모니터링", "", f"기준: {crawled}"],
        [""],
    ]

    if not df_all.empty:
        # 단계별 현황
        dash += [["▶ 단계별 현황"], ["단계", "전체", "토목", "기타"]]
        for stage in STAGE_ORDER:
            s_df = df_all[df_all["current_stage"] == stage]
            dash.append([
                stage,
                len(s_df),
                len(s_df[s_df["interest"] == "토목"]),
                len(s_df[s_df["interest"] == "기타"]),
            ])
        dash.append(["합계", len(df_all),
                      len(df_all[df_all["interest"] == "토목"]),
                      len(df_all[df_all["interest"] == "기타"])])
        dash.append([""])

        # D-Day 임박
        urgent = df_all[df_all["deadline"].apply(
            lambda x: calc_dday(x).startswith("D-") and
                       calc_dday(x) not in ("", "D-Day") and
                       int(calc_dday(x).replace("D-", "")) <= 7
            if isinstance(x, str) and x else False
        )]
        dash += [["▶ 마감 임박 (7일내)", f"{len(urgent)}건"]]
        if not urgent.empty:
            dash.append(["사업명", "발주처", "마감일", "D-Day", "단계"])
            for _, r in urgent.head(10).iterrows():
                dash.append([
                    r["project_name"], r["client_org"],
                    r["deadline"], calc_dday(r["deadline"]), r["current_stage"],
                ])
        dash.append([""])

        # 발주처 TOP 10
        top_clients = df_all["client_org"].value_counts().head(10)
        dash += [["▶ 발주처별 건수 (TOP 10)"], ["발주처", "건수"]]
        for client, cnt in top_clients.items():
            dash.append([client, int(cnt)])
        dash.append([""])

        # 이번주 단계 변경
        if not df_changes.empty:
            dash += [["▶ 최근 단계 변경", f"{len(df_changes)}건"],
                     ["시간", "사업명", "변경", "발주처", "분류"]]
            for _, r in df_changes.head(15).iterrows():
                prev = r.get("prev_stage", "")
                change = f"{prev} → {r['stage']}" if prev else f"신규 ({r['stage']})"
                dash.append([
                    r["detected_at"][:16], r["project_name"],
                    change, r["client_org"], r.get("interest", ""),
                ])

    ws.clear()
    ws.update(dash, value_input_option="USER_ENTERED")
    logger.info("[📊 대시보드] 업로드 완료")

    # ── 2) 전체현황 ──
    if not df_all.empty:
        ws2 = _get_or_create_ws(sh, "📋 전체현황", 1000, 15)
        display = df_all.copy()
        display["D-Day"] = display["deadline"].apply(calc_dday)
        display["추정가_표시"] = display["est_price"].apply(fmt_amount)
        display["계약금_표시"] = display["contract_amt"].apply(fmt_amount)

        cols = ["bid_ntce_no", "project_name", "client_org", "current_stage",
                "announce_date", "deadline", "D-Day", "추정가_표시",
                "계약금_표시", "contractor", "interest", "detail_url"]
        headers = ["공고번호", "사업명", "발주처", "현재단계",
                   "공고일", "마감일", "D-Day", "추정가격",
                   "계약금액", "계약업체", "분류", "상세URL"]

        existing_cols = [c for c in cols if c in display.columns]
        out = display[existing_cols].fillna("").astype(str)
        header_map = dict(zip(cols, headers))
        out.columns = [header_map.get(c, c) for c in existing_cols]

        ws2.clear()
        ws2.update([out.columns.tolist()] + out.values.tolist(),
                    value_input_option="USER_ENTERED")
        logger.info(f"[📋 전체현황] {len(out)}행 업로드")

    # ── 3) 변경알림 ──
    if not df_changes.empty:
        ws3 = _get_or_create_ws(sh, "🔔 변경알림", 500, 8)
        ch = df_changes.copy()
        ch["변경"] = ch.apply(
            lambda r: f"{r.get('prev_stage', '')} → {r['stage']}"
                      if r.get("prev_stage") else f"신규 ({r['stage']})", axis=1)
        out_ch = ch[["detected_at", "bid_ntce_no", "project_name",
                      "변경", "client_org", "interest"]].fillna("").astype(str)
        out_ch.columns = ["감지일시", "공고번호", "사업명",
                          "단계변경", "발주처", "분류"]
        ws3.clear()
        ws3.update([out_ch.columns.tolist()] + out_ch.values.tolist(),
                    value_input_option="USER_ENTERED")
        logger.info(f"[🔔 변경알림] {len(out_ch)}행 업로드")

    # ── 4) 토목 전용 탭 ──
    if not df_all.empty:
        civil = df_all[df_all["interest"] == "토목"]
        if not civil.empty:
            ws4 = _get_or_create_ws(sh, "🚧 토목공사", 500, 12)
            civil_out = civil[["bid_ntce_no", "project_name", "client_org",
                                "current_stage", "announce_date", "deadline",
                                "est_price", "contractor"]].copy()
            civil_out["D-Day"] = civil_out["deadline"].apply(calc_dday)
            civil_out["추정가_표시"] = civil_out["est_price"].apply(fmt_amount)
            civil_out = civil_out.drop(columns=["est_price"]).fillna("").astype(str)
            civil_out.columns = ["공고번호", "사업명", "발주처", "현재단계",
                                  "공고일", "마감일", "계약업체", "D-Day", "추정가격"]
            ws4.clear()
            ws4.update([civil_out.columns.tolist()] + civil_out.values.tolist(),
                        value_input_option="USER_ENTERED")
            logger.info(f"[🚧 토목공사] {len(civil_out)}행 업로드")


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────
def cmd_add_client(names: list[str]):
    with db_conn() as con:
        for n in names:
            con.execute("INSERT OR IGNORE INTO watch_clients (name) VALUES (?)", (n,))
    logger.info(f"[관심 발주처 추가] {names}")


def cmd_list_clients():
    with db_conn() as con:
        rows = con.execute("SELECT name FROM watch_clients ORDER BY name").fetchall()
    if rows:
        print("관심 발주처 목록:")
        for r in rows:
            print(f"  - {r['name']}")
    else:
        print("등록된 관심 발주처가 없습니다.")
    # 환경변수도 표시
    if WATCH_CLIENTS:
        print(f"\n.env WATCH_CLIENTS: {', '.join(WATCH_CLIENTS)}")


def cmd_stats():
    with db_conn() as con:
        total = con.execute("SELECT COUNT(*) c FROM projects").fetchone()["c"]
        by_stage = con.execute(
            "SELECT current_stage, COUNT(*) c FROM projects GROUP BY current_stage"
        ).fetchall()
        by_interest = con.execute(
            "SELECT interest, COUNT(*) c FROM projects GROUP BY interest"
        ).fetchall()
    print(f"\n전체 추적 사업: {total}건")
    print("\n단계별:")
    for r in by_stage:
        print(f"  {r['current_stage']}: {r['c']}건")
    print("\n분류별:")
    for r in by_interest:
        print(f"  {r['interest'] or '미분류'}: {r['c']}건")


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="발주처별 계약 단계 모니터링")
    parser.add_argument("--days", type=int, default=7,
                        help="수집 기간 (기본: 7일)")
    parser.add_argument("--no-upload", action="store_true",
                        help="구글 시트 업로드 생략")
    parser.add_argument("--client", type=str,
                        help="특정 발주처만 필터")
    parser.add_argument("--add-client", nargs="+",
                        help="관심 발주처 추가")
    parser.add_argument("--list-clients", action="store_true",
                        help="관심 발주처 목록 조회")
    parser.add_argument("--stats", action="store_true",
                        help="DB 통계 조회")
    args = parser.parse_args()

    init_db()

    if args.add_client:
        cmd_add_client(args.add_client)
        return
    if args.list_clients:
        cmd_list_clients()
        return
    if args.stats:
        cmd_stats()
        return

    logger.info(f"===== 모니터링 시작: {now_kst():%Y-%m-%d %H:%M:%S KST} =====")

    # 특정 발주처 임시 필터
    if args.client:
        global WATCH_CLIENTS
        WATCH_CLIENTS = [args.client]
        logger.info(f"[필터] 발주처: {args.client}")

    # 수집 + DB 저장
    new_cnt, changed_cnt = collect_and_save(days_back=args.days)

    # DB에서 전체 조회
    df_all = get_all_projects()
    df_changes = get_stage_changes(since_days=args.days)

    if df_all.empty:
        logger.warning("수집된 데이터 없음")
        logger.info("===== 완료 =====")
        return

    # 로컬 Excel 저장
    today_str = now_kst().strftime("%Y%m%d")
    excel_path = f"./monitor_{today_str}.xlsx"
    with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
        df_all.to_excel(writer, sheet_name="전체현황", index=False)
        if not df_changes.empty:
            df_changes.to_excel(writer, sheet_name="변경알림", index=False)
        civil = df_all[df_all["interest"] == "토목"]
        if not civil.empty:
            civil.to_excel(writer, sheet_name="토목공사", index=False)
    logger.info(f"[로컬 저장] {excel_path}")

    # Google Sheets 업로드
    if not args.no_upload:
        gc = get_gc()
        upload_sheets(gc, df_all, df_changes)
    else:
        logger.info("[업로드 생략]")

    logger.info(f"===== 완료: 전체 {len(df_all)}건 / 신규 {new_cnt} / 변경 {changed_cnt} =====")


if __name__ == "__main__":
    main()
