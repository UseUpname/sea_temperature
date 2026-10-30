"""
고수온 선제 상담 대시보드 - '어가 상세' API

대시보드(HTML)의 '어가 상세' 화면을 채우는 백엔드입니다.
모든 데이터는 customer_service.fishery_households 의 어가(household_id)를 기준으로 모읍니다.

데이터 출처
  - 어가          : customer_service.fishery_households (연락처 phone_number·sms_consent 포함)
  - 어가 위치     : public.sigungu_area_coordinates (fishery_households.region_code = region_id)
  - 양식 어종     : customer_service.household_species + fish_species  (1어종 1계약 단위로 카드 구성)
  - 계약별 보험   : 첨부한 보험금 계산 API(aqua-claim-api)의 household_overview
                    (계약·보험목적·가입금액·특약·자기부담비율·보험료 분납안)
                    → 어종(mapping_id)별로 '현재 유효 계약'을 골라 고수온 보장 여부 판정
  - 보장 판정     : insurance.insurance_coverages (주계약/특약, 보장추가/보장제외, risk_types)
  - 추천 상품     : insurance.insurance_map(어종 → 상품) + insurance.insurance_products(판매기간 등)
  - 오늘 수온     : 국립수산과학원 RISA 실시간 관측 (home_test.py 방식, 인증키 없는 화면용 엔드포인트)
                    → 실패하면 오늘 ROMS 예측값
  - 1~7일 예측    : public.example_region_daily      (ROMS, 지역별 일 최고/평균/최저)
  - 8~21일 예측   : public.example_region_daily_om   (ECMWF 보정, 흐름 참고)
  - 상담 기록     : customer_service.consult_cases / consult_logs / consult_log_items

위험 단계 (대시보드 '판정 참고 기준'과 동일, 일 최고 수온 기준)
  관심 : 25℃ 도달 예상
  주의 : 28℃ 도달 예상
  경보 : 28℃ 이상 3일 연속
  - 확정 단계는 1~7일(ROMS) 기준. 오늘 값은 실측이 예측보다 높으면 실측을 씀.
    연속 일수는 테이블에 있는 어제·그제 값까지 이어서 셉니다.
  - 8~21일(ECMWF 보정)은 같은 기준으로 '참고 단계'만 따로 계산합니다.
    확정 단계에 포함하려면 STAGE_INCLUDE_OM=1
  - 어종별 견디는 한계 수온(SPECIES_LIMIT)에 7일 안에 닿으면 그 어종 카드는 최소 '주의'.

엔드포인트
  GET  /api/households/health                   보험금 계산 API 연결 상태 (기존 /health 는 백엔드 것과 겹쳐 이름 변경)
  GET  /api/households?q=&limit=                어가 목록 (상세 화면의 어가 선택용)
  GET  /api/households/{household_id}/detail    어가 상세 전체
  POST /api/households/{household_id}/consult-logs    상담 결과 기록
  GET  /api/households/{household_id}/consult         상담 기록만 새로 받기 (통화 녹음 STT 결과 포함)
  POST /api/households/{household_id}/claim-estimate  고수온 피해 시 예상 보험금 (보험금 계산 API 위임)

실행 (2026-10-26 sooui9: 기존 백엔드 8000에 라우터로 붙이도록 변경)
  pip install psycopg_pool httpx
  backend/backend/main.py 에서 이 파일의 router 를 등록해 기존 백엔드(8000)와 함께 실행
  DB 접속은 기존 백엔드 설정(backend/backend/.env)을 그대로 씀
  보험금 계산 API(aqua-claim-api)는 backend/aqua-claim-api 폴더에서 자동으로 찾음
  화면 기본 주소, 대시보드 HTML 띄우기, CORS 는 기존 백엔드(main.py)가 맡음

※ TODO 표시는 실제 데이터를 보고 확정해야 하는 부분입니다.
"""

from __future__ import annotations

import importlib
import json
import logging
import math
import os
import re
import sys
import threading
import time
from contextvars import ContextVar
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Optional
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from backend.config import get_settings
from backend.farm_detail_query_cache import cache_detail_request, cache_repository_query

log = logging.getLogger("household_detail_api")
logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)

KST = ZoneInfo("Asia/Seoul")

# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
DB_POOL_MAX = int(os.getenv("DB_POOL_MAX", "5"))

AQUA_CLAIM_API_DIR = os.getenv("AQUA_CLAIM_API_DIR", str(Path(__file__).with_name("aqua-claim-api")))
AQUA_CLAIM_API_URL = os.getenv("AQUA_CLAIM_API_URL", "").rstrip("/")
CLAIM_API_TIMEOUT_SEC = 10.0

# 실시간 수온 (RISA) — home_test.py 와 같은 방식
RISA_BASE_URL = os.getenv("RISA_BASE_URL", "https://www.nifs.go.kr/risa/")
RISA_TIMEOUT_SEC = 8.0
RISA_CACHE_SEC = 5 * 60
RISA_FAIL_CACHE_SEC = 60
RISA_LIST_MAX_PAGES = 20
RISA_DEBUG = os.getenv("RISA_DEBUG") == "1"
OBS_STALE_HOURS = 3                                         # 이보다 오래된 관측값은 '오늘 실측'으로 안 씀
REALTIME_MAX_KM = float(os.getenv("REALTIME_MAX_KM", "15"))  # 어가에서 이 거리 안 관측소만 사용
REGION_CACHE_SEC = 60 * 60
REGION_FAR_KM = 30         # 예측 지역이 이보다 멀면 '대표성 낮음' 표시

# 예측
FORECAST_DAYS = 7          # 1~7일 (오늘 포함) : example_region_daily
EXTENDED_DAYS = 21         # 8~21일            : example_region_daily_om
STAGE_METRIC = os.getenv("STAGE_METRIC", "max_c")          # 단계 판정에 쓸 값: max_c(일최고) | mean_c
STAGE_INCLUDE_OM = os.getenv("STAGE_INCLUDE_OM") == "1"     # 8~21일까지 확정 단계에 포함할지
TREND_DEG = 0.5            # 2~3주 흐름: 앞 3일 평균과 뒤 3일 평균 차이가 이보다 크면 상승/하강

# 판정 참고 기준
WATCH_TEMP = 25.0          # 관심: 25℃ 도달 예상
CAUTION_TEMP = 28.0        # 주의: 28℃ 도달 예상
ALERT_TEMP = 28.0          # 경보: 28℃ 이상
ALERT_STREAK_DAYS = 3      #       3일 연속
STAGE = ["정상", "관심", "주의", "경보"]

# 국립수산과학원(2024.7.19) 품종별 생존 상한. 자료 없는 어종은 지역 기준만 적용
SPECIES_LIMIT = {"조피볼락": 28.0, "우럭": 28.0, "전복": 28.0, "넙치": 29.0, "광어": 29.0, "강도다리": 27.0}

# 고수온을 보장하는 재해명 (insurance_coverages.risk_types)
HEAT_PERILS = ("고수온", "이상수온")
# 저수온을 보장하는 재해명 (같은 보장 표·같은 판정 규칙으로 판정)
COLD_PERILS = ("저수온",)

# ---------------------------------------------------------------------------
# 고수온 / 저수온 모드 (화면 오른쪽 위 모드 선택 → /detail?mode=heat|cold)
#   고수온: 일최고 수온 · 관심 25℃ · 주의 28℃ · 경보 28℃ 이상 3일 · 어종 = SPECIES_LIMIT
#   저수온: 일최저 수온 · 관심 7℃ · 주의 4℃ · 경보 4℃ 이하 3일 (수과원 저수온 특보 기준)
#           어종 = 수협 수산경제연구원(2026.1) 먹이 중단 수온 (농어는 폐사 5~8℃ → 8℃)
#   모드는 요청마다 ContextVar 로 정해서, 아래 함수들이 M() 으로 그 기준을 꺼내 씀
# ---------------------------------------------------------------------------
SPECIES_COLD_LIMIT = {"조피볼락": 5.0, "우럭": 5.0, "농어": 8.0, "참돔": 10.0, "감성돔": 10.0, "숭어": 10.0,
                      "돌돔": 13.0, "쥐치": 15.0, "능성어": 15.0}
MODE_RULES = {
    "heat": {"id": "heat", "word": "고수온", "hot": True, "val": "최고", "watch": WATCH_TEMP, "caution": CAUTION_TEMP,
             "alert": ALERT_TEMP, "perils": HEAT_PERILS, "limits": SPECIES_LIMIT, "limit_word": "견디는 한계"},
    "cold": {"id": "cold", "word": "저수온", "hot": False, "val": "최저", "watch": 7.0, "caution": 4.0,
             "alert": 4.0, "perils": COLD_PERILS, "limits": SPECIES_COLD_LIMIT, "limit_word": "먹이 끊는 수온"},
}
_MODE: ContextVar[str] = ContextVar("farm_detail_mode", default="heat")


def M() -> dict:
    """지금 요청의 모드 기준 (기본 고수온)."""
    return MODE_RULES[_MODE.get()]


def reach(v: float, limit: float) -> bool:
    """기준에 닿았나: 고수온은 이상, 저수온은 이하."""
    return v >= limit if M()["hot"] else v <= limit


# 저수온 모드에서 안내 문구를 바꿀 말 (고수온 → 저수온 은 tx() 가 모두 바꿈)
COLD_PHRASES = (
    ("오를 것으로", "내려갈 것으로"),
    ("넘는 날이", "밑도는 날이"),
    ("경보 시 먹이 주기 중단 · 선별·분조 금지 (약관상 의무)", "저수온 특보 시 먹이량 조절 · 선별·이동 자제 (약관 의무는 수협 확인)"),
    ("경보가 나면 먹이 주기 중단, 선별·분조 금지 (약관상 의무)", "저수온 특보가 나면 먹이량 조절, 선별·이동 자제 (약관 의무는 수협 확인)"),
    ("경보가 나면 먹이 주기를 멈추고 선별·분조는 미뤄 주셔야 해요.", "저수온 특보가 나면 먹이량을 줄이고 선별·이동은 미뤄 주세요."),
    ("경보 때 지켜야 할 의무를 안내하고", "저수온 때 지켜야 할 대응을 안내하고"),
    ("액화산소 등 대응 장비 점검 안내", "월동 대비(사육밀도·먹이량 조절) 안내"),
)


def tx(o: Any) -> Any:
    """저수온 모드면 문구 속 '고수온'·수온 방향 말을 저수온에 맞게 바꿈 (dict·list 안까지)."""
    if M()["hot"]:
        return o
    if isinstance(o, str):
        for a, b in COLD_PHRASES:
            o = o.replace(a, b)
        return o.replace("고수온", "저수온")
    if isinstance(o, list):
        return [tx(x) for x in o]
    if isinstance(o, dict):
        return {k: tx(v) for k, v in o.items()}
    return o

# 고수온 피해 시 예상 보험금: 직원 입력 없이 피해율별로 자동 계산
#   보유량·체중 정보가 DB 에 없으므로 '보험가액 = 가입금액'(전부보험)으로 가정하고
#   손해액 = 가입금액 × 피해율 로 보험금 계산 API 를 호출 (자기부담·상품유형 규칙은 API 그대로 적용)
HEAT_SCENARIO_RATES = tuple(float(x) for x in os.getenv("HEAT_SCENARIO_RATES", "0.1,0.3,0.5,1.0").split(","))

# 공식 특보 (DB 테이블이 아직 없어 환경변수로 옮겨 적음)
#   예) OFFICIAL_ALERTS='{"고성": {"type": "고수온 예비특보", "since": "10/21"}}'
# TODO: 수과원 특보 테이블이 생기면 DB 조회로 바꾸기
OFFICIAL_ALERTS: dict[str, dict] = json.loads(os.getenv("OFFICIAL_ALERTS", "{}") or "{}")

# 상담 기록: consult_cases.season (어가 × 시즌 1건)
CONSULT_SEASON = os.getenv("CONSULT_SEASON", "")            # 비우면 'YYYY-고수온'

ACTIVE_CONTRACT = "유효"
PENDING_CONTRACT = "청약"
RENEW_NOTICE_DAYS = 45     # 만기 이 기간 안이면 갱신 안내


def today_kst() -> date:
    return datetime.now(KST).date()


def consult_season(d: date) -> str:
    return CONSULT_SEASON or f"{d.year}-고수온"   # consult.py SEASON("2026-고수온")과 같은 형식이어야 같은 consult_cases 행을 씀


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------
pool: Optional[ConnectionPool] = None
http = httpx.Client(timeout=RISA_TIMEOUT_SEC)


_pool_lock = threading.Lock()


def db() -> ConnectionPool:
    """처음 요청 때 DB 연결 묶음을 만듦. 접속 정보는 기존 백엔드 설정(backend/backend/.env)."""
    global pool
    if pool is None:
        with _pool_lock:
            if pool is None:
                p = ConnectionPool("", min_size=1, max_size=DB_POOL_MAX, open=True,
                                   # Supabase pooler 는 prepared statement 미지원.
                                   # autocommit: 조회 1건마다 BEGIN·COMMIT 왕복을 빼서 3배 빠름. 쓰기는 conn.transaction() 으로 묶음
                                   kwargs={**get_settings().connection_kwargs(), "prepare_threshold": None,
                                           "autocommit": True})
                claim_api.setup(p)
                pool = p
                log.info("DB pool ready")
    return pool


def q_all(sql: str, params: Any = None) -> list[dict]:
    with db().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def q_one(sql: str, params: Any = None) -> Optional[dict]:
    rows = q_all(sql, params)
    return rows[0] if rows else None


def to_json(o: Any) -> Any:
    """Decimal·date 를 JSON 으로. 금액(정수 Decimal)은 int 로, 비율은 float 로."""
    if isinstance(o, Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    if isinstance(o, datetime):
        return o.astimezone(KST).isoformat() if o.tzinfo else o.isoformat()
    if isinstance(o, date):
        return o.isoformat()
    if isinstance(o, dict):
        return {k: to_json(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set)):
        return [to_json(v) for v in o]
    return o


# ---------------------------------------------------------------------------
# 보험금 계산 API (aqua-claim-api) 연결
#   1) AQUA_CLAIM_API_URL 이 있으면 HTTP 로 호출
#   2) 없으면 AQUA_CLAIM_API_DIR 의 app 패키지를 불러와 같은 DB 풀로 바로 호출
# ---------------------------------------------------------------------------
class ClaimApiError(Exception):
    def __init__(self, status: int, detail: Any):
        super().__init__(str(detail))
        self.status, self.detail = status, detail


CLAIM_PKG = "aqua_claim_app"

# ---------------------------------------------------------------------------
# 적용가격 조회 호환 (실제 DB 컬럼 타입이 aqua-claim-api 의 sql/001 과 다름)
#   DB: region_no integer, month/valid_from/valid_to/base_price varchar, price/weight_min_g real
#   API: region_no text('7','8','전권역'), month smallint, valid_* date, base_price numeric
#   → 어떤 타입이든 SQL 안에서 text/date/int/numeric 으로 바꿔 비교하고, API 가 기대하는 타입으로 돌려줌
#   region_no 가 NULL 인 행(정수 컬럼에 '전권역'을 못 넣은 경우)은 전권역으로 간주
# ---------------------------------------------------------------------------
def _sql_date(col: str) -> str:
    return (f"(CASE WHEN {col}::text ~ '^\\d{{4}}[-./]\\d{{1,2}}[-./]\\d{{1,2}}' "
            f"THEN to_date(regexp_replace(substr({col}::text, 1, 10), '[./]', '-', 'g'), 'YYYY-MM-DD') END)")


def _sql_int(col: str) -> str:
    return f"NULLIF(regexp_replace({col}::text, '\\D', '', 'g'), '')::int"


def _sql_num(col: str) -> str:
    return f"NULLIF(regexp_replace({col}::text, '[^0-9.]', '', 'g'), '')::numeric"


_PRICE_COLS = f"""price::numeric AS price, unit, weight_min_g::numeric AS weight_min_g, {_sql_int('month')} AS month,
    variant, COALESCE(region_no::text, '전권역') AS region_no, size_label,
    {_sql_date('valid_from')} AS valid_from, {_sql_date('valid_to')} AS valid_to,
    notice_month, section, source, note"""
_PRICE_WHERE = f"""product_id = %(product_id)s AND species_id = %(species_id)s
    AND (region_no::text IN (%(region_no)s, '전권역') OR region_no IS NULL)
    AND ({_sql_date('valid_from')} IS NULL OR {_sql_date('valid_from')} <= %(on_date)s)
    AND ({_sql_date('valid_to')} IS NULL OR {_sql_date('valid_to')} >= %(on_date)s)"""


def make_compat_repository(base: type) -> type:
    class CompatRepository(base):   # type: ignore[misc, valid-type]
        @cache_repository_query
        def _all(self, sql, params=None):
            return super()._all(sql, params)

        def find_price(self, *, product_id, species_id, region_no, on_date, weight_g, variant, month=None):
            rows = self._all(
                f"""SELECT {_PRICE_COLS} FROM insurance.insurance_applied_price
                    WHERE {_PRICE_WHERE}
                      AND ({_sql_int('month')} IS NULL OR {_sql_int('month')} = %(month)s)
                      AND (%(variant)s::text IS NULL OR variant = %(variant)s)
                      AND (CASE WHEN %(weight_g)s::numeric IS NULL THEN weight_min_g IS NULL
                                ELSE weight_min_g IS NULL OR weight_min_g::numeric <= %(weight_g)s::numeric END)
                    ORDER BY weight_min_g DESC NULLS LAST, notice_month DESC
                    LIMIT 1""",
                dict(product_id=product_id, species_id=species_id, region_no=str(region_no), on_date=on_date,
                     month=month or on_date.month, variant=variant, weight_g=weight_g))
            return rows[0] if rows else None

        def list_prices(self, product_id, species_id, region_no, on_date):
            return self._all(f"SELECT {_PRICE_COLS} FROM insurance.insurance_applied_price WHERE {_PRICE_WHERE}",
                             dict(product_id=product_id, species_id=species_id, region_no=str(region_no), on_date=on_date))

        def list_formulas(self, product_id, on_date):
            return self._all(
                f"""SELECT section, product_id, variant, applies, formula, {_sql_num('base_price')} AS base_price,
                           period_end::text AS period_end, floor, region_label,
                           {_sql_date('valid_from')} AS valid_from, {_sql_date('valid_to')} AS valid_to, source
                    FROM insurance.insurance_applied_price_formula
                    WHERE product_id = %(p)s
                      AND ({_sql_date('valid_from')} IS NULL OR {_sql_date('valid_from')} <= %(d)s)
                      AND ({_sql_date('valid_to')} IS NULL OR {_sql_date('valid_to')} >= %(d)s)""",
                dict(p=product_id, d=on_date))
    return CompatRepository


def claim_dir_candidates() -> list[Path]:
    """aqua-claim-api 폴더 후보. 환경변수 → py 파일 옆 → 상위 폴더 → 실행 폴더 순."""
    here = Path(__file__).resolve().parent
    raw = [os.getenv("AQUA_CLAIM_API_DIR")] if os.getenv("AQUA_CLAIM_API_DIR") else []
    out: list[Path] = []
    for r in raw:
        p = Path(r).expanduser()
        out += [p] if p.is_absolute() else [Path.cwd() / p, here / p]
    out += [here / "aqua-claim-api", here.parent / "aqua-claim-api", Path.cwd() / "aqua-claim-api"]
    seen, uniq = set(), []
    for p in out:
        k = str(p.resolve())
        if k not in seen:
            seen.add(k)
            uniq.append(p.resolve())
    return uniq


def find_claim_app_dir() -> Optional[Path]:
    """후보 폴더에서 app/service.py 를 찾음. 압축을 풀며 한 단계 더 들어간 경우(aqua-claim-api/aqua-claim-api)도 확인."""
    for base in claim_dir_candidates():
        for cand in (base / "app", base / "aqua-claim-api" / "app", base):
            if (cand / "service.py").exists() and (cand / "engine.py").exists():
                return cand
    return None


def load_package(name: str, pkg_dir: Path) -> str:
    """pkg_dir 를 name 이라는 패키지로 등록 (내부는 상대 import 라 이름이 달라도 동작)."""
    if name not in sys.modules:
        import types
        mod = types.ModuleType(name)
        mod.__path__ = [str(pkg_dir)]       # __init__.py 가 없어도 패키지로 동작
        mod.__package__ = name
        sys.modules[name] = mod
    return name


class ClaimApi:
    def __init__(self) -> None:
        self.mode = "none"
        self.status_detail: Optional[str] = None
        self._service = self._schemas = self._repo = None
        self._http: Optional[httpx.Client] = None

    def setup(self, conn_pool: ConnectionPool) -> None:
        if AQUA_CLAIM_API_URL:
            self.mode = "http"
            self._http = httpx.Client(base_url=AQUA_CLAIM_API_URL, timeout=CLAIM_API_TIMEOUT_SEC)
            self.status_detail = AQUA_CLAIM_API_URL
            log.info("보험금 계산 API: HTTP %s", AQUA_CLAIM_API_URL)
            return
        app_dir = find_claim_app_dir()
        self.status_detail = None
        if app_dir is None:
            self.status_detail = "aqua-claim-api/app/service.py 를 찾지 못함 (확인한 경로: " + \
                                 ", ".join(str(p) for p in claim_dir_candidates()) + ")"
            log.warning("보험금 계산 API 미연결: %s", self.status_detail)
            return
        try:
            # 'app' 이라는 이름은 다른 프로젝트 패키지와 겹치기 쉬워서 별도 이름으로 불러옴
            pkg = load_package(CLAIM_PKG, app_dir)
            self._service = importlib.import_module(f"{pkg}.service")
            self._schemas = importlib.import_module(f"{pkg}.schemas")
            self._repo = make_compat_repository(importlib.import_module(f"{pkg}.repository").PgRepository)(conn_pool)
            self.mode = "import"
            self.status_detail = str(app_dir)
            log.info("보험금 계산 API: 모듈 직접 호출 (%s)", app_dir)
        except Exception as e:
            self.status_detail = f"불러오기 실패 ({app_dir}): {type(e).__name__}: {e}"
            log.exception("aqua-claim-api 불러오기 실패")

    def close(self) -> None:
        if self._http:
            self._http.close()

    def household_overview(self, household_id: int) -> Optional[dict]:
        """계약 목록. 계약이 하나도 없으면 {'contracts': []}. 연결 안 됐으면 None."""
        if self.mode == "import":
            try:
                return to_json(self._service.household_overview(self._repo, household_id))
            except self._service.ServiceError as e:
                if e.status == 404:
                    return {"contracts": []}
                raise ClaimApiError(e.status, e.message)
            except Exception as e:   # 계산 API 쪽 오류로 상세 화면 전체가 실패하지 않게 → DB 기본 정보로 대체
                log.warning("보험금 계산 API(household_overview) 오류: %s", e)
                return None
        if self.mode == "http":
            try:
                res = self._http.get(f"/households/{household_id}/insurance")
            except httpx.HTTPError as e:
                log.warning("보험금 계산 API 호출 실패: %s", type(e).__name__)
                return None
            if res.status_code == 404:
                return {"contracts": []}
            if res.status_code >= 400:
                raise ClaimApiError(res.status_code, res.json().get("detail", res.text))
            return res.json()
        return None

    def estimate_sim(self, target: dict, payload: dict) -> dict:
        """가상 계약(target)으로 계산: 실제 계약 대신 target 을 돌려주는 저장소로 같은 계산 규칙 사용 (모듈 연결 때만)."""
        if self.mode != "import":
            raise ClaimApiError(503, {"detail": "모듈 연결 때만 가능"})
        repo = self._repo

        class SimRepo:
            def __getattr__(self, name):
                return getattr(repo, name)

            def get_target(self, insurance_id, mapping_id):
                return dict(target)
        try:
            return to_json(self._service.estimate(SimRepo(), self._schemas.EstimateRequest(**payload)))
        except self._service.ServiceError as e:
            raise ClaimApiError(e.status, {"detail": e.message, **to_json(e.extra)})

    def estimate(self, payload: dict) -> dict:
        if self.mode == "import":
            try:
                req = self._schemas.EstimateRequest(**payload)
                return to_json(self._service.estimate(self._repo, req))
            except self._service.ServiceError as e:
                raise ClaimApiError(e.status, {"detail": e.message, **to_json(e.extra)})
            except ValueError as e:   # pydantic 검증 오류
                raise ClaimApiError(422, {"detail": str(e)})
            except Exception as e:
                log.exception("보험금 계산 API(estimate) 오류")
                raise ClaimApiError(500, {"detail": f"보험금 계산 중 오류: {type(e).__name__}: {str(e).splitlines()[0][:200]}"})
        if self.mode == "http":
            res = self._http.post("/claims/estimate", json=to_json(payload))
            body = res.json()
            if res.status_code >= 400:
                raise ClaimApiError(res.status_code, body)
            return body
        raise ClaimApiError(503, {"detail": "보험금 계산 API 가 연결되지 않았어요 (AQUA_CLAIM_API_DIR 또는 AQUA_CLAIM_API_URL)"})


claim_api = ClaimApi()


# ---------------------------------------------------------------------------
# 1. 어가 · 어종
# ---------------------------------------------------------------------------
HOUSEHOLD_SQL = """
SELECT h.household_id, h.household_name, h.owner_name, h.fishery_type, h.license_number, h.region_code,
       s.sigungu_name, s.area_name, s.latitude::float8 AS lat, s.longitude::float8 AS lon,
       h.phone_number, h.sms_consent          -- 연락처 컬럼은 fishery_households 로 옮겨짐
FROM customer_service.fishery_households h
LEFT JOIN public.sigungu_area_coordinates s ON s.region_id = h.region_code
WHERE h.household_id = %s
"""

SPECIES_SQL = """
SELECT hs.mapping_id, hs.species_id, hs.is_primary, hs.production_ratio, hs.status,
       fs.standard_name, fs.common_name, fs.category, fs.farming_method, fs.life_stage
FROM customer_service.household_species hs
JOIN customer_service.fish_species fs ON fs.species_id = hs.species_id
WHERE hs.household_id = %s
ORDER BY hs.is_primary DESC, hs.production_ratio DESC NULLS LAST, hs.mapping_id
"""

# 보험금 계산 API 가 없을 때 쓰는 최소 계약 조회 (같은 테이블)
CONTRACTS_FALLBACK_SQL = """
SELECT c.insurance_id, c.policy_number, c.product_id, p.product_name, c.start_date, c.end_date,
       c.contract_status AS status, c.total_premium, t.mapping_id, t.coverage_amount, t.rider_details
FROM customer_service.insurance_contracts c
JOIN customer_service.insurance_targets t ON t.insurance_id = c.insurance_id
LEFT JOIN insurance.insurance_products p ON p.product_id = c.product_id
WHERE c.household_id = %s
ORDER BY c.start_date DESC
"""


def mask_phone(p: Optional[str]) -> Optional[str]:
    if not p:
        return None
    d = re.sub(r"\D", "", p)
    return f"{d[:3]}-****-{d[-4:]}" if len(d) >= 10 else p


def normalize_sigungu(name: Optional[str]) -> Optional[str]:
    """'통영시' → '통영', '부산광역시' → '부산'."""
    if not name:
        return None
    n = name.strip().replace(" ", "")
    for suffix in ("광역시", "특별시", "시", "군", "구"):
        if n.endswith(suffix) and len(n) > len(suffix):
            return n[: -len(suffix)]
    return n


def species_label(sp: dict) -> str:
    nm, cm = sp.get("standard_name") or "", sp.get("common_name") or ""
    return f"{nm}({cm})" if cm and cm != nm else nm


# ---------------------------------------------------------------------------
# 2. 예측 지역 · 예측값 · 위험 단계
# ---------------------------------------------------------------------------
_region_cache: dict[str, tuple[float, list[dict]]] = {}


def forecast_regions(table: str) -> list[dict]:
    """public.roms_regions / public.om_regions (1시간 캐시)."""
    hit = _region_cache.get(table)
    if hit and time.monotonic() < hit[0]:
        return hit[1]
    rows = q_all(f"SELECT region, station, station_code, lat::float8 AS lat, lon::float8 AS lon, status FROM public.{table}")
    _region_cache[table] = (time.monotonic() + REGION_CACHE_SEC, rows)
    return rows


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def pick_region(regions: list[dict], sigungu: Optional[str], lat: Optional[float], lon: Optional[float]) -> Optional[dict]:
    """시군 이름이 같은 지역 우선, 없으면 어가 좌표에서 가장 가까운 지역."""
    short = normalize_sigungu(sigungu)
    if short:
        for r in regions:
            if normalize_sigungu(r["region"]) == short or r["region"].startswith(short):
                km = haversine_km(lat, lon, r["lat"], r["lon"]) if lat is not None and lon is not None else None
                return {**r, "km": round(km, 1) if km is not None else None, "matched": "name"}
    if lat is None or lon is None or not regions:
        return None
    best = min(regions, key=lambda r: haversine_km(lat, lon, r["lat"], r["lon"]))
    return {**best, "km": round(haversine_km(lat, lon, best["lat"], best["lon"]), 1), "matched": "nearest"}


def fetch_daily(table: str, region: str, start: date, end: date) -> list[dict]:
    rows = q_all(
        f"""SELECT day, mean_c::float8 AS mean, max_c::float8 AS max, min_c::float8 AS min, dist_km::float8 AS dist_km
            FROM public.{table}
            WHERE region = %s AND day BETWEEN %s AND %s
            ORDER BY day""",
        (region, start, end),
    )
    return [{k: (round(v, 1) if isinstance(v, float) else v) for k, v in r.items()} for r in rows]


def data_range(table: str, region: Optional[dict]) -> dict:
    if not region:
        return {"region": None, "reason": "예측 지역을 찾지 못함 (어가 region_code·좌표 없음)"}
    r = q_one(f"SELECT min(day) AS first, max(day) AS last, count(*) AS n FROM public.{table} WHERE region = %s",
              (region["region"],))
    if not r or not r["n"]:
        return {"region": region["region"], "reason": f"{table} 에 '{region['region']}' 지역 자료가 없음"}
    return {"region": region["region"], "first": r["first"], "last": r["last"], "n": r["n"],
            "reason": f"'{region['region']}' 자료가 {r['first']} ~ {r['last']} 만 있음 (오늘 이후 자료 없음)"}


def metric(row: dict) -> Optional[float]:
    if not M()["hot"]:   # 저수온은 일최저로 판정
        v = row.get("min")
        return v if v is not None else row.get("mean")
    v = row.get("max" if STAGE_METRIC == "max_c" else "mean")
    return v if v is not None else row.get("max")


def day_stages(values: list[Optional[float]], lookback: list[Optional[float]]) -> list[dict]:
    """날짜별 단계. lookback = 오늘 이전 값(연속 일수 계산용)."""
    seq = list(lookback) + list(values)
    out, run = [], 0
    for i, t in enumerate(seq):
        run = run + 1 if t is not None and reach(t, M()["alert"]) else 0
        if i < len(lookback):
            continue
        if t is None:
            s = 0
        elif reach(t, M()["alert"]) and run >= ALERT_STREAK_DAYS:
            s = 3
        elif reach(t, M()["caution"]):
            s = 2
        elif reach(t, M()["watch"]):
            s = 1
        else:
            s = 0
        out.append({"stage": s, "streak28": run})
    return out


def summarize_stage(days: list[dict], today: date) -> dict:
    """가장 높은 단계와 그 단계가 처음 나오는 날."""
    if not days:
        return {"level": None, "label": "정보 없음", "first_date": None, "d": None, "temp": None}
    top = max(d["stage"] for d in days)
    first = next(d for d in days if d["stage"] == top)
    return {"level": top, "label": STAGE[top], "first_date": first["day"], "d": (first["day"] - today).days,
            "temp": first["value"]}


def trend_of(series: list[dict]) -> Optional[str]:
    vals = [metric(r) for r in series if metric(r) is not None]
    if len(vals) < 4:
        return None
    head, tail = sum(vals[:3]) / 3, sum(vals[-3:]) / 3
    return "up" if tail - head > TREND_DEG else "down" if head - tail > TREND_DEG else "flat"


# ---------------------------------------------------------------------------
# 3. 오늘 수온: 국립수산과학원 RISA 실시간 (home_test.py 의 방식을 그대로 옮김)
#   https://www.nifs.go.kr/risa/selectTodayObsvtrList.do  pageNo=N   → todayObsvtrList (전체 관측소 최신값)
#   https://www.nifs.go.kr/risa/selectTodayObsvtrMap.do   obsvtrCd=X → todayInfo (관측소 1곳)
#   ※ 공식 Open API 가 아닌 화면용 엔드포인트라 예고 없이 바뀔 수 있음 → 실패 시 오늘 예측값
# ---------------------------------------------------------------------------
RISA_TEMP_KEYS = ("wtem", "wtrTmp", "wtrTemp", "srfWtrTmp", "surWtrTmp", "wtrTmpSrf", "waterTemp", "wtr_tmp", "tmp")
RISA_DATETIME_KEYS = ("obsrvnDt", "obsDtm", "obsDt", "obsrDt", "obsvtnDt", "obsDatetime", "regDt")
RISA_DATE_KEYS = ("obsDate", "obsDe", "obsDay", "obs_dat")
RISA_TIME_KEYS = ("obsTime", "obsTm", "obsHour", "obs_tim")
RISA_LAYER_KEYS = ("obsLay", "obsLayer", "obsvtrLayer", "layer", "obs_lay")
RISA_NAME_KEYS = ("obsvtrKornNm", "obsvtrNm", "obsvtrName", "staNm", "sta_nam_kor")
RISA_REPAIR_KEYS = ("rprYn", "rpr_yn", "repairYn")
RISA_CODE_KEYS = ("obsvtrCd", "staCde", "sta_cde")
RISA_LAT_KEYS = ("lat", "latitude", "la")
RISA_LON_KEYS = ("lot", "lon", "lng", "longitude", "lo")

_DT_RE = re.compile(r"(\d{4})[-./]?(\d{2})[-./]?(\d{2})[ T]?(\d{2}):?(\d{2})(?::?(\d{2}))?")
_risa_lock = threading.Lock()
_live_cache: tuple[float, list[dict]] = (float("-inf"), [])
_coord_cache: tuple[float, dict[str, dict]] = (float("-inf"), {})
_station_cache: dict[str, tuple[float, Optional[dict]]] = {}


def _first(d: dict, keys: tuple[str, ...]) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def _to_float(v: Any) -> Optional[float]:
    try:
        f = float(str(v).replace("℃", "").strip())
    except (TypeError, ValueError):
        return None
    return f if -5.0 <= f <= 40.0 else None


def _find_temp(d: dict) -> Optional[float]:
    v = _to_float(_first(d, RISA_TEMP_KEYS))
    if v is not None:
        return v
    keys = [k for k in d if re.search(r"tmp|temp|wtem", k, re.I) and not re.search(r"air|atm|ta$", k, re.I)]
    keys.sort(key=lambda k: 0 if re.search(r"sur|srf|top|1$", k, re.I) else 1)
    for k in keys:
        if (v := _to_float(d[k])) is not None:
            return v
    return None


def _find_time(d: dict) -> Optional[datetime]:
    raw = _first(d, RISA_DATETIME_KEYS)
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) or (isinstance(raw, str) and raw.isdigit() and len(raw) == 13):
        v = float(raw)
        v = v / 1000 if v > 1e11 else v
        if 1e9 < v < 4e9:
            return datetime.fromtimestamp(v, tz=KST)
    if raw is None:
        day, tm = _first(d, RISA_DATE_KEYS), _first(d, RISA_TIME_KEYS)
        if day is not None and tm is not None:
            raw = f"{day} {tm}"
    candidates = ([raw] if raw is not None else []) + [v for v in d.values() if isinstance(v, str)]
    for c in candidates:
        m = _DT_RE.search(str(c))
        if m:
            y, mo, da, h, mi, s = m.groups()
            try:
                return datetime(int(y), int(mo), int(da), int(h), int(mi), int(s or 0), tzinfo=KST)
            except ValueError:
                continue
    return None


def parse_today_info(info: Any) -> Optional[dict]:
    """todayInfo(dict 또는 층별 list) → {"temp", "observed_at", "name"} (표층 최신값, 점검 중 제외)."""
    rows = [r for r in (info if isinstance(info, list) else [info]) if isinstance(r, dict)]
    best: Optional[dict] = None
    for r in rows:
        lay = _first(r, RISA_LAYER_KEYS)
        if lay is not None and str(lay).strip() not in ("1", "표층", "S", "SUR", "sur"):
            continue
        if str(_first(r, RISA_REPAIR_KEYS) or "N").upper() == "Y":
            continue
        if re.search(r"점검|보수|고장|결측|수리", str(r.get("obsrvnExpln") or "")):
            continue
        temp, obs = _find_temp(r), _find_time(r)
        if temp is None or obs is None:
            continue
        if best is None or obs > best["observed_at"]:
            best = {"temp": round(temp, 1), "observed_at": obs, "name": _first(r, RISA_NAME_KEYS)}
    return best


def _position(r: dict) -> tuple[Optional[float], Optional[float]]:
    def c(v: Any, lo: float, hi: float) -> Optional[float]:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f if lo <= f <= hi else None
    return c(_first(r, RISA_LAT_KEYS), 32.0, 39.5), c(_first(r, RISA_LON_KEYS), 124.0, 132.0)


def risa_post(path: str, params: dict) -> Any:
    res = http.post(RISA_BASE_URL + path, data=params, headers={
        "User-Agent": "Mozilla/5.0", "Referer": RISA_BASE_URL + "main.do",
        "Content-Type": "application/x-www-form-urlencoded"})
    res.raise_for_status()
    return res.json()


def risa_station_coords() -> dict[str, dict]:
    """관측소코드 → 위치. roms_regions/om_regions 의 station_code + RISA 관측소 목록 (1시간 캐시)."""
    global _coord_cache
    if time.monotonic() < _coord_cache[0]:
        return _coord_cache[1]
    coords: dict[str, dict] = {}
    for table in ("roms_regions", "om_regions"):
        for r in forecast_regions(table):
            if r.get("station_code"):
                coords[str(r["station_code"])] = {"lat": r["lat"], "lon": r["lon"], "name": r.get("station") or r["region"]}
    try:
        payload = risa_post("risa/risaC/searchRisaStationList.do", {"pageNo": 1, "rowCountPage": 500})
        for r in (payload.get("retList") or []) if isinstance(payload, dict) else []:
            code = str(_first(r, RISA_CODE_KEYS) or "").strip()
            lat, lon = _position(r)
            if code and lat is not None and lon is not None:
                coords[code] = {"lat": lat, "lon": lon, "name": _first(r, RISA_NAME_KEYS)}
    except (httpx.HTTPError, ValueError) as e:
        log.warning("RISA 관측소 목록 조회 실패: %s", type(e).__name__)
    _coord_cache = (time.monotonic() + REGION_CACHE_SEC, coords)
    return coords


def risa_live_list() -> list[dict]:
    """지금 값이 있는 RISA 관측소 전체 [{sta_cde, sta_name, lat, lon, obs}] (5분 캐시)."""
    global _live_cache
    with _risa_lock:
        if time.monotonic() < _live_cache[0]:
            return _live_cache[1]
        rows: list[dict] = []
        seen: set[str] = set()
        ok = False
        try:
            for page in range(1, RISA_LIST_MAX_PAGES + 1):
                payload = risa_post("selectTodayObsvtrList.do", {"pageNo": page})
                page_rows = (payload.get("todayObsvtrList") or []) if isinstance(payload, dict) else []
                new = [r for r in page_rows if isinstance(r, dict) and str(_first(r, RISA_CODE_KEYS) or "") not in seen]
                if not new:
                    break
                seen.update(str(_first(r, RISA_CODE_KEYS) or "") for r in new)
                rows.extend(new)
            ok = True
        except (httpx.HTTPError, ValueError) as e:
            log.warning("RISA 최신 수온 목록 조회 실패: %s", type(e).__name__)

        live: list[dict] = []
        if rows:
            if RISA_DEBUG:
                log.info("RISA todayObsvtrList %d건, 첫 항목: %s", len(rows), json.dumps(rows[0], ensure_ascii=False)[:1000])
            coords = risa_station_coords() if any(None in _position(r) for r in rows) else {}
            for r in rows:
                code = str(_first(r, RISA_CODE_KEYS) or "").strip()
                lat, lon = _position(r)
                if (lat is None or lon is None) and code in coords:
                    lat, lon = coords[code]["lat"], coords[code]["lon"]
                obs = parse_today_info(r)
                if code and lat is not None and lon is not None and obs:
                    live.append({"sta_cde": code, "sta_name": obs.get("name") or coords.get(code, {}).get("name") or code,
                                 "lat": lat, "lon": lon, "obs": obs})
        _live_cache = (time.monotonic() + (RISA_CACHE_SEC if ok else RISA_FAIL_CACHE_SEC), live)
        return live


def risa_station_today(code: str) -> Optional[dict]:
    """관측소 1곳 최신 표층 수온 (목록 호출이 실패했을 때 대체 경로)."""
    hit = _station_cache.get(code)
    if hit and time.monotonic() < hit[0]:
        return hit[1]
    result, ok = None, False
    try:
        payload = risa_post("selectTodayObsvtrMap.do", {"obsvtrCd": code})
        info = payload.get("todayInfo") if isinstance(payload, dict) else None
        result = parse_today_info(info) if info else None
        ok = True
    except (httpx.HTTPError, ValueError) as e:
        log.warning("RISA %s 조회 실패: %s", code, type(e).__name__)
    _station_cache[code] = (time.monotonic() + (RISA_CACHE_SEC if ok else RISA_FAIL_CACHE_SEC), result)
    return result


def resolve_realtime(lat: Optional[float], lon: Optional[float], roms_region: Optional[dict]) -> Optional[dict]:
    """어가에서 REALTIME_MAX_KM 안, 3시간 안에 관측된 가장 가까운 관측소 값.
    반환: {"temp", "observed_at", "station_name", "station_km"} 또는 None"""
    fresh_after = datetime.now(KST) - timedelta(hours=OBS_STALE_HOURS)
    if lat is not None and lon is not None:
        live = risa_live_list()
        ranked = sorted(
            ({**s, "km": round(haversine_km(lat, lon, s["lat"], s["lon"]), 1)}
             for s in live if s["obs"]["observed_at"] >= fresh_after),
            key=lambda s: s["km"])
        if ranked and ranked[0]["km"] <= REALTIME_MAX_KM:
            b = ranked[0]
            return {"temp": b["obs"]["temp"], "observed_at": b["obs"]["observed_at"],
                    "station_name": b["sta_name"], "station_km": b["km"]}
        if live:   # 목록은 받았는데 가까운 관측소가 없음 → 멀리 있는 값은 쓰지 않음
            return None
    # 목록 호출 실패 → 예측 지역에 적힌 관측소(station_code)로 직접 조회
    if roms_region and roms_region.get("station_code"):
        o = risa_station_today(str(roms_region["station_code"]))
        if o and o["observed_at"] >= fresh_after:
            km = (haversine_km(lat, lon, roms_region["lat"], roms_region["lon"])
                  if lat is not None and lon is not None else None)
            if km is None or km <= REALTIME_MAX_KM:
                return {"temp": o["temp"], "observed_at": o["observed_at"],
                        "station_name": o.get("name") or roms_region.get("station"),
                        "station_km": round(km, 1) if km is not None else None}
    return None


def build_sea(hh: dict, today: date) -> dict:
    """오늘 수온 + 1~7일 ROMS + 8~21일 ECMWF 보정 + 단계."""
    lat, lon = hh.get("lat"), hh.get("lon")
    roms_r = pick_region(forecast_regions("roms_regions"), hh.get("sigungu_name"), lat, lon)
    om_r = pick_region(forecast_regions("om_regions"), hh.get("sigungu_name"), lat, lon)

    last7 = today + timedelta(days=FORECAST_DAYS - 1)
    roms_all = fetch_daily("example_region_daily", roms_r["region"], today - timedelta(days=ALERT_STREAK_DAYS - 1), last7) if roms_r else []
    past = [r for r in roms_all if r["day"] < today]
    roms = [r for r in roms_all if r["day"] >= today]
    om = fetch_daily("example_region_daily_om", om_r["region"], last7 + timedelta(days=1),
                     today + timedelta(days=EXTENDED_DAYS - 1)) if om_r else []

    # 오늘 수온: RISA 실측 → 없으면 오늘 ROMS 예측
    try:
        rt = resolve_realtime(lat, lon, roms_r)
    except Exception as e:  # 외부 호출 문제로 상세 화면 전체가 실패하지 않게
        log.warning("실시간 수온 처리 중 오류: %s", e)
        rt = None
    today_fc = next((r for r in roms if r["day"] == today), None)
    if rt:
        now = {"temp": rt["temp"], "source": "risa", "is_forecast": False,
               "observed_at": rt["observed_at"].strftime("%m/%d %H:%M"),
               "station_name": rt["station_name"], "station_km": rt["station_km"]}
    elif today_fc:
        now = {"temp": metric(today_fc), "source": "forecast", "is_forecast": True,
               "observed_at": today.strftime("%m/%d") + " 예측", "station_name": None, "station_km": None}
    else:
        now = {"temp": None, "source": "none", "is_forecast": False, "observed_at": None,
               "station_name": None, "station_km": None}

    # 단계 판정 값: 오늘은 실측이 예측보다 위험하면 실측 사용 (고수온은 더 높으면, 저수온은 더 낮으면)
    def vals(series: list[dict], use_obs: bool) -> list[Optional[float]]:
        out = []
        for r in series:
            v = metric(r)
            if use_obs and rt and r["day"] == today and (v is None or (rt["temp"] > v if M()["hot"] else rt["temp"] < v)):
                v = rt["temp"]
            out.append(v)
        return out

    roms_vals = vals(roms, True)
    roms_st = day_stages(roms_vals, [metric(r) for r in past])
    roms_days = [{**r, "value": v, **s, "src": "ROMS"} for r, v, s in zip(roms, roms_vals, roms_st)]

    om_vals = vals(om, False)
    om_st = day_stages(om_vals, roms_vals[-(ALERT_STREAK_DAYS - 1):])   # 7일차에서 이어지는 연속 일수
    om_days = [{**r, "value": v, **s, "src": "ECMWF"} for r, v, s in zip(om, om_vals, om_st)]

    stage = summarize_stage(roms_days + (om_days if STAGE_INCLUDE_OM else []), today)
    stage_ext = summarize_stage(roms_days + om_days, today)
    if M()["hot"]:
        hi = max(roms_days, key=lambda d: (d["value"] if d["value"] is not None else -99, -d["day"].toordinal()), default=None)
    else:   # 저수온은 7일 중 가장 낮은 날
        hi = min(roms_days, key=lambda d: (d["value"] if d["value"] is not None else 99, d["day"].toordinal()), default=None)

    short = normalize_sigungu(hh.get("sigungu_name"))
    alert = OFFICIAL_ALERTS.get(short or "") or (OFFICIAL_ALERTS.get(roms_r["region"]) if roms_r else None)
    return {
        "region": roms_r["region"] if roms_r else None,
        "region_match": roms_r.get("matched") if roms_r else None,
        "region_km": roms_r.get("km") if roms_r else None,
        # 이름이 같은 예측 지역이 없어 가장 가까운 곳을 썼는데 멀리 있으면 화면에 표시
        "region_far": bool(roms_r and roms_r.get("matched") == "nearest" and (roms_r.get("km") or 0) > REGION_FAR_KM),
        "om_region": om_r["region"] if om_r else None,
        "mode": M()["id"],
        "metric": ("일최고" if STAGE_METRIC == "max_c" else "일평균") if M()["hot"] else "일최저",
        "today": now,
        "roms": roms_days,
        "om": om_days,
        # 예측이 비었을 때 원인 확인용: 테이블에 실제로 있는 날짜 범위
        "data_range": {"roms": data_range("example_region_daily", roms_r) if not roms_days else None,
                       "om": data_range("example_region_daily_om", om_r) if not om_days else None},
        # 7일 중 가장 위험한 날 (고수온 = 최고 · 저수온 = 최저)
        "max7": {"temp": hi["value"], "date": hi["day"], "d": (hi["day"] - today).days} if hi and hi["value"] is not None else None,
        "stage": stage,                        # 확정 단계 (1~7일)
        "stage_extended": stage_ext,           # 참고 단계 (1~21일)
        "trend": trend_of(om),                 # 2~3주 흐름 up | flat | down
        "alert": alert,                        # 공식 특보 (없으면 None)
        "criteria": {"watch": M()["watch"], "caution": M()["caution"], "alert": M()["alert"], "alert_days": ALERT_STREAK_DAYS,
                     "include_om": STAGE_INCLUDE_OM},
    }


def species_risk(sp: dict, sea: dict, today: date) -> dict:
    name = f"{sp.get('standard_name') or ''} {sp.get('common_name') or ''}"
    R = M()
    limit = next((v for k, v in R["limits"].items() if k in name), None)
    label = sp.get("standard_name") or ""
    if limit is None:
        return {"known": False, "hit": False, "stage": 0, "limit": None,
                "short": "기준 확인 필요",
                "text": f"{label} 한계 수온 자료 없음 · 지역 기준 적용" if R["hot"] else f"{label} 저수온 기준 자료 없음 · 지역 기준 적용"}
    days = [d for d in sea["roms"] if d["value"] is not None]
    hit = next((d for d in days if reach(d["value"], limit)), None)
    if hit:
        d = (hit["day"] - today).days
        return {"known": True, "hit": True, "stage": 2, "limit": limit, "d": d, "date": hit["day"],
                "short": f"{limit:g}℃ 도달 D-{d}",
                "text": f"{label} {R['limit_word']} {limit:g}℃ · {'오늘' if d == 0 else f'{d}일 뒤'} 도달 예상"}
    vals = [d["value"] for d in days]
    ext = (max(vals) if R["hot"] else min(vals)) if vals else None
    gap = (round(limit - ext, 1) if R["hot"] else round(ext - limit, 1)) if ext is not None else None
    return {"known": True, "hit": False, "stage": 0, "limit": limit, "gap": gap,
            "short": f"{limit:g}℃까지 {gap:.1f}℃" if gap is not None else f"기준 {limit:g}℃",
            "text": f"{label} {R['limit_word']} {limit:g}℃" + (f" · 7일 {R['val']}보다 {gap:.1f}℃ 여유" if gap is not None else "")}


# ---------------------------------------------------------------------------
# 4. 계약별 보험 (고수온 보장 판정 · 추천)
# ---------------------------------------------------------------------------
COVERAGES_SQL = """
SELECT product_id, coverage_key, contract_type, name, risk_types, effect
FROM insurance.insurance_coverages
WHERE product_id = ANY(%s)
ORDER BY product_id, coverage_key
"""

PRODUCTS_SQL = """
SELECT product_id, product_name, species, sale_period, insurance_period, government_subsidy, deductible,
       premium_payment_method, contacts
FROM insurance.insurance_products
WHERE product_id = ANY(%s)
"""

MAP_SQL = """
SELECT m.species_id, m.product_id
FROM insurance.insurance_map m
WHERE m.species_id = ANY(%s) AND m.coverage_target = '수산물'
ORDER BY m.product_id
"""

INS = {   # 화면 배지 (HTML 의 INS 와 같은 키)
    "full":    "가입(고수온 보장)",
    "excl":    "가입 · 고수온 부담보",
    "norider": "특약 없음",
    "noheat":  "가입 · 고수온 미보장 상품",
    "pending": "청약 중",
    "expired": "만료",
    "none":    "미가입",
    "unknown": "모름",
}
GAP_CODES = ("none", "expired", "norider", "excl", "unknown")   # 상담으로 메울 수 있는 공백
URGENCY = {"none": 0, "expired": 0, "unknown": 1, "norider": 1, "excl": 2, "noheat": 3, "pending": 4, "full": 5}


def _nospace(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def _peril_in(peril: str, risk_types: list[str]) -> bool:
    """aqua-claim-api engine._peril_in 과 같은 규칙."""
    p = peril.replace(" ", "")
    for r in risk_types or []:
        r2 = r.replace(" ", "")
        if r2 == p or r2.startswith(p + "(") or p in r2.split("(")[0].split("/") or p in r2.split("·"):
            return True
    return False


def heat_profile(coverages: list[dict], perils: tuple = HEAT_PERILS) -> dict:
    """상품의 고수온(perils=COLD_PERILS 면 저수온) 보장 구조: 주계약 포함 / 특약으로 추가 / 부담보 특약."""
    def is_heat(c: dict) -> Optional[str]:
        rt = c["risk_types"] if isinstance(c["risk_types"], list) else json.loads(c["risk_types"] or "[]")
        return next((p for p in perils if _peril_in(p, rt)), None)
    main, riders, excl = [], [], []
    for c in coverages:
        peril = is_heat(c)
        if not peril:
            continue
        item = {"key": c["coverage_key"], "name": c["name"], "peril": peril}
        if c["effect"] == "보장추가":
            (main if c["contract_type"] == "주계약" else riders).append(item)
        elif c["effect"] == "보장제외":
            excl.append(item)
    peril = (main or riders or excl or [{"peril": perils[0]}])[0]["peril"]
    return {"main": main, "riders": riders, "excl": excl, "peril": peril}


def heat_status(profile: dict, joined_riders: list[str], perils: tuple = HEAT_PERILS,
                label: str = "고수온") -> tuple[str, dict]:
    """계약의 가입 특약(rider_details.riders) 기준으로 고수온(label='저수온'이면 저수온) 보장 판정."""
    joined = {_nospace(r) for r in joined_riders}
    # 이름이 조금 달라도 잡히게: 특약 이름에 재해명(고수온/이상수온 또는 저수온) + 부담보 여부로 보조 판정
    loose_cover = [r for r in joined_riders if any(p in r for p in perils) and "부담보" not in r]
    loose_excl = [r for r in joined_riders if any(p in r for p in perils) and "부담보" in r]
    rider_in = [c for c in profile["riders"] if _nospace(c["name"]) in joined] or \
               [{"name": r, "peril": profile["peril"]} for r in loose_cover]
    excl_in = [c for c in profile["excl"] if _nospace(c["name"]) in joined] or \
              [{"name": r, "peril": profile["peril"]} for r in loose_excl]
    if excl_in:
        return "excl", {"ok": False, "via": "부담보", "text": f"{excl_in[0]['name']} 가입 → {label} 손해는 보상하지 않아요"}
    if profile["main"]:
        return "full", {"ok": True, "via": "주계약", "text": f"주계약에 {profile['main'][0]['peril']} 보장 포함"}
    if rider_in:
        return "full", {"ok": True, "via": "특약", "text": f"{rider_in[0]['name']} 가입 → {label} 보장"}
    if profile["riders"]:
        return "norider", {"ok": False, "via": None,
                           "text": f"{label} 미보장 · {profile['riders'][0]['name']}을 추가해야 보장"}
    return "noheat", {"ok": False, "via": None, "text": f"이 상품에는 {label} 보장(주계약·특약)이 없어요"}


def heat_offer_text(profile: dict, label: str = "고수온") -> dict:
    """미가입 어종에 상품을 추천할 때 고수온(label='저수온'이면 저수온) 보장 방식 안내."""
    if profile["main"]:
        t = f"주계약에 {profile['main'][0]['peril']} 포함" + (" (부담보 특약으로 뺄 수 있음)" if profile["excl"] else "")
        return {"ok": True, "via": "주계약", "text": t}
    if profile["riders"]:
        return {"ok": None, "via": "특약", "text": f"{label}은 {profile['riders'][0]['name']}을 함께 넣어야 보장"}
    return {"ok": False, "via": None, "text": f"이 상품에는 {label} 보장이 없어요"}


_MONTH_RANGE_RE = re.compile(r"(\d{1,2})\s*월?\s*[~∼\-–]\s*(?:익년|다음\s*해|이듬해)?\s*(\d{1,2})\s*월")
_MONTH_RE = re.compile(r"(\d{1,2})\s*월")
_DATE_RANGE_RE = re.compile(r"(\d{4})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})\s*일?\s*[~∼\-–]\s*"
                            r"(\d{4})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})")


def sale_state(sale_period: Optional[str], today: date) -> dict:
    """insurance_products.sale_period(자유 텍스트) → 지금 가입 가능 여부.
    TODO: 실제 sale_period 값 형식을 보고 규칙 확정 (SELECT DISTINCT sale_period ...)"""
    text = (sale_period or "").strip()
    if not text:
        return {"code": "unknown", "text": "판매기간 확인 필요", "raw": None}
    m = _DATE_RANGE_RE.search(text)
    if m:
        y1, m1, d1, y2, m2, d2 = map(int, m.groups())
        try:
            start, end = date(y1, m1, d1), date(y2, m2, d2)
        except ValueError:
            start = end = None
        if start and end:
            if today < start:
                return {"code": "later", "text": f"{start.month}/{start.day}부터", "start_month": start.month, "raw": text}
            if today > end:
                return {"code": "closed", "text": "판매 종료", "raw": text}
            soon = (end - today).days <= 31
            return {"code": "soon" if soon else "open", "text": f"{end.month}/{end.day} 마감" if soon else "가능",
                    "end_month": end.month, "end_date": end, "raw": text}
    months: set[int] = set()
    for a, b in _MONTH_RANGE_RE.findall(text):
        a, b = int(a), int(b)
        if 1 <= a <= 12 and 1 <= b <= 12:
            k = a
            while True:
                months.add(k)
                if k == b:
                    break
                k = k % 12 + 1
    if not months:
        months = {int(x) for x in _MONTH_RE.findall(text) if 1 <= int(x) <= 12}
    if not months:
        return {"code": "unknown", "text": "판매기간 확인 필요", "raw": text}
    m = today.month
    if m in months:
        nxt = m % 12 + 1
        if nxt in months:
            return {"code": "open", "text": "가능", "raw": text, "months": sorted(months)}
        return {"code": "soon", "text": "이번 달 마감", "end_month": m, "raw": text, "months": sorted(months)}
    k = m
    for _ in range(12):
        k = k % 12 + 1
        if k in months:
            break
    return {"code": "later", "text": f"{k}월부터", "start_month": k, "raw": text, "months": sorted(months)}


def load_contracts(household_id: int) -> tuple[list[dict], str]:
    """보험금 계산 API 의 household_overview → 계약 목록. 반환: (contracts, source)."""
    try:
        ov = claim_api.household_overview(household_id)
    except ClaimApiError as e:
        log.warning("보험금 계산 API 오류 (%s): %s → DB 기본 정보로 대체", e.status, e.detail)
        ov = None
    if ov is not None:
        return ov.get("contracts") or [], "claim_api"
    # 대체: 같은 테이블에서 최소 정보만
    contracts: dict[int, dict] = {}
    for r in q_all(CONTRACTS_FALLBACK_SQL, (household_id,)):
        rd = r["rider_details"] if isinstance(r["rider_details"], dict) else json.loads(r["rider_details"] or "{}")
        c = contracts.setdefault(r["insurance_id"], {
            "insurance_id": r["insurance_id"], "policy_number": r["policy_number"], "product_id": r["product_id"],
            "product_name": r["product_name"], "start_date": r["start_date"], "end_date": r["end_date"],
            "status": r["status"], "calc_supported": None,
            "premium": {"total_premium": r["total_premium"], "installment": bool(rd.get("installment")), "plans": []},
            "targets": []})
        c["targets"].append({"mapping_id": r["mapping_id"], "coverage_amount": r["coverage_amount"],
                             "plan": rd.get("plan_type"), "deductible_rate": rd.get("deductible_rate"),
                             "min_deductible_rate": rd.get("min_deductible_rate"),
                             "riders": list(rd.get("riders") or []), "installment": bool(rd.get("installment")),
                             "warnings": []})
    return to_json(list(contracts.values())), "db"


def _d(v: Any) -> Optional[date]:
    if v is None or isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def pick_contract(entries: list[tuple[dict, dict]], today: date) -> tuple[Optional[tuple[dict, dict]], str]:
    """어종 1개에 걸린 계약들 중 지금 기준 계약 1건 + 상태 (active | pending | expired | none)."""
    def in_force(c: dict) -> bool:
        return c["status"] == ACTIVE_CONTRACT and _d(c["start_date"]) <= today <= _d(c["end_date"])
    active = [e for e in entries if in_force(e[0])]
    if active:
        return max(active, key=lambda e: _d(e[0]["start_date"])), "active"
    pending = [e for e in entries if e[0]["status"] == PENDING_CONTRACT and _d(e[0]["end_date"]) >= today]
    if pending:
        return max(pending, key=lambda e: _d(e[0]["start_date"])), "pending"
    future = [e for e in entries if e[0]["status"] == ACTIVE_CONTRACT and _d(e[0]["start_date"]) > today]
    if future:   # 보험기간이 아직 시작 전
        return min(future, key=lambda e: _d(e[0]["start_date"])), "pending"
    if entries:
        return max(entries, key=lambda e: _d(e[0]["end_date"])), "expired"
    return None, "none"


REF_AMOUNT_SQL = """
SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY t.coverage_amount) AS amount, count(*) AS n,
       mode() WITHIN GROUP (ORDER BY t.rider_details->>'plan_type') AS plan,
       mode() WITHIN GROUP (ORDER BY t.rider_details->>'deductible_rate') AS rate
FROM customer_service.insurance_targets t
JOIN customer_service.insurance_contracts c ON c.insurance_id = t.insurance_id
JOIN customer_service.household_species hs ON hs.mapping_id = t.mapping_id
WHERE c.product_id = %(p)s AND t.coverage_amount > 0
  AND (%(s)s::int IS NULL OR hs.species_id = %(s)s::int)
"""


def reference_terms(product_id: int, species_id: int) -> Optional[dict]:
    """미가입 어종의 '가입했다면' 계산용 가입 조건: 같은 상품·어종 가입 어가의 중간값 → 없으면 같은 상품 전체."""
    for sp, label in ((species_id, "같은 어종 가입 어가 중간값"), (None, "같은 상품 가입 어가 중간값")):
        r = q_one(REF_AMOUNT_SQL, {"p": product_id, "s": sp})
        if r and r["n"] and r["amount"]:
            return {"amount": Decimal(str(round(r["amount"]))), "basis": f"{label} ({r['n']}건)",
                    "plan": r["plan"] or "표준형", "rate": r["rate"] or "0.2"}
    return None


def _run_scenarios(estimate, amount: Decimal, peril: str, ids: tuple[int, int], today: date) -> list[dict]:
    """피해율별 보험금 (보험가액 = 가입금액, 손해액 = 가입금액 × 피해율)."""
    rows = []
    for r in HEAT_SCENARIO_RATES:
        dmg = amount * Decimal(str(r))
        res = estimate({"insurance_id": ids[0], "mapping_id": ids[1], "accident_date": today, "peril": peril,
                        "aquatic": {"held_count": amount, "damaged_count": dmg,
                                    "applied_price_override": Decimal(1), "applied_price_unit": "KRW_PER_FISH"},
                        "damage": {"damage_rate": Decimal(str(r)), "in_harvest": True}})
        loss = round(float(dmg))
        if not res.get("covered"):
            rows.append({"rate": r, "loss": loss, "payout": 0, "deductible": None, "burden": loss,
                         "reasons": res.get("not_covered_reasons") or []})
            continue
        x = res["result"]
        loss = x["loss"] if x.get("loss") is not None else loss
        rows.append({"rate": r, "loss": loss, "payout": x["payout"], "deductible": x["deductible"],
                     "burden": loss - x["payout"]})
    return rows


def heat_scenarios(card: dict, today: date, hh: dict) -> Optional[dict]:
    """고수온 피해 시 보험금 표.
    - 고수온 보장 가입(full)      : kind=insured  · 받는 보험금
    - 미가입·만료·특약 없음·부담보 : kind=compare  · 지금 받는 보험금(0원) vs 가입(특약 추가)하면 받는 보험금
    - 그 밖(청약 중·상품에 고수온 없음) : kind=uncovered · 손해 전액 어가 부담"""
    code, c, p = card["ins"], card.get("contract"), card.get("product") or {}
    peril = (card.get("heat") or {}).get("peril") or M()["perils"][0]
    try:
        if code == "full" and c and c.get("status") == ACTIVE_CONTRACT:
            if not c.get("coverage_amount"):
                return {"kind": "insured", "error": "가입금액 정보가 없어 계산할 수 없어요", "rows": []}
            if claim_api.mode == "none":
                return {"kind": "insured", "error": "보험금 계산 API 미연결", "rows": []}
            amount = Decimal(str(c["coverage_amount"]))
            return {"kind": "insured", "amount": amount,
                    "rows": _run_scenarios(claim_api.estimate, amount, peril, (c["insurance_id"], card["mapping_id"]), today)}

        if code in ("none", "expired", "norider", "excl") and p.get("product_id"):
            heat_rider = (p.get("heat_riders") or [None])[0]
            if code in ("norider", "excl") and c:
                # 지금 계약 그대로 + 고수온 특약 추가 / 부담보 특약 제거
                amount = Decimal(str(c["coverage_amount"] or 0))
                riders = [r for r in c.get("riders") or [] if not (any(h in r for h in M()["perils"]) and "부담보" in r)]
                if code == "norider" and heat_rider:
                    riders.append(heat_rider)
                terms = {"plan": c.get("plan") or "표준형", "rate": c.get("deductible_rate") or "0.2",
                         "min_rate": c.get("min_deductible_rate"),
                         "basis": "지금 계약 가입금액", "action": f"{M()['word']} 특약을 넣으면" if code == "norider" else "부담보 특약을 빼면"}
            else:
                prev = card.get("expired") or {}
                ref = ({"amount": Decimal(str(prev["coverage_amount"])), "basis": "이전 계약 가입금액",
                        "plan": "표준형", "rate": "0.2"} if prev.get("coverage_amount")
                       else reference_terms(p["product_id"], card["species_id"]))
                if not ref:
                    return {"kind": "compare", "error": "비교할 가입금액 자료가 없어요", "rows": []}
                amount = ref["amount"]
                riders = [heat_rider] if heat_rider and not p.get("heat_main") else []
                terms = {**ref, "action": "가입하면"}
            if not amount:
                return {"kind": "compare", "error": "가입금액 정보가 없어 계산할 수 없어요", "rows": []}
            now_rows = [{"rate": r, "loss": round(float(amount) * r)} for r in HEAT_SCENARIO_RATES]
            if claim_api.mode != "import":
                return {"kind": "compare", "amount": amount, "basis": terms["basis"], "action": terms["action"],
                        "rows": [{**x, "payout": None} for x in now_rows],
                        "error": "가입 시 금액은 보험금 계산 API 를 모듈로 연결했을 때만 계산돼요"}
            target = {
                "insurance_id": -1, "household_id": hh["household_id"], "product_id": p["product_id"],
                "policy_number": "SIM", "total_premium": None, "start_date": today, "end_date": today + timedelta(days=364),
                "contract_status": ACTIVE_CONTRACT, "mapping_id": card["mapping_id"], "coverage_amount": amount,
                "rider_details": {"riders": riders, "plan_type": terms["plan"], "deductible_rate": terms["rate"],
                                  **({"min_deductible_rate": terms["min_rate"]} if terms.get("min_rate") else {})},
                "species_id": card["species_id"], "production_ratio": card.get("production_ratio"),
                "species_status": card.get("species_status"), "standard_name": card["standard_name"],
                "common_name": None, "farming_method": card.get("method"), "household_name": hh.get("household_name"),
                "sigungu_name": hh.get("sigungu_name"), "area_name": hh.get("area_name"), "product_name": p.get("product_name"),
            }
            rows = _run_scenarios(lambda pl: claim_api.estimate_sim(target, pl), amount, peril, (-1, card["mapping_id"]), today)
            return {"kind": "compare", "amount": amount, "basis": terms["basis"], "action": terms["action"],
                    "product_name": p.get("product_name"), "rider": heat_rider if riders else None,
                    "rows": [{"rate": r["rate"], "loss": r["loss"], "now": 0, "payout": r["payout"],
                              "deductible": r["deductible"]} for r in rows]}

        if c and c.get("coverage_amount") and code in ("noheat",):
            amount = float(c["coverage_amount"])
            return {"kind": "uncovered", "amount": c["coverage_amount"],
                    "rows": [{"rate": r, "loss": round(amount * r), "payout": 0, "burden": round(amount * r)}
                             for r in HEAT_SCENARIO_RATES]}
    except (ClaimApiError, httpx.HTTPError) as e:
        detail = e.detail.get("detail") if isinstance(getattr(e, "detail", None), dict) else str(e)
        return {"kind": "compare" if code != "full" else "insured", "error": f"자동 계산 불가: {detail}", "rows": []}
    return None


def build_cards(household_id: int, species: list[dict], sea: dict, today: date, hh: dict) -> tuple[list[dict], str]:
    contracts, source = load_contracts(household_id)

    by_mapping: dict[int, list[tuple[dict, dict]]] = {}
    for c in contracts:
        for t in c.get("targets") or []:
            by_mapping.setdefault(t["mapping_id"], []).append((c, t))

    # 추천 후보 상품 (어종 → 상품)
    sp_ids = [s["species_id"] for s in species]
    cand: dict[int, list[int]] = {}
    for r in q_all(MAP_SQL, (sp_ids,)) if sp_ids else []:
        cand.setdefault(r["species_id"], []).append(r["product_id"])

    product_ids = sorted({pid for v in cand.values() for pid in v} | {c["product_id"] for c in contracts})
    products = {r["product_id"]: r for r in q_all(PRODUCTS_SQL, (product_ids,))} if product_ids else {}
    covs: dict[int, list[dict]] = {}
    for r in q_all(COVERAGES_SQL, (product_ids,)) if product_ids else []:
        covs.setdefault(r["product_id"], []).append(r)
    # 모드의 재해(고수온 또는 저수온) 기준 보장 구조 · 이름은 heat_* 그대로지만 저수온 모드면 저수온 판정
    profiles = {pid: heat_profile(covs.get(pid, []), M()["perils"]) for pid in product_ids}

    def product_info(pid: Optional[int]) -> Optional[dict]:
        if pid is None:
            return None
        p = products.get(pid, {})
        prof = profiles.get(pid) or heat_profile([])
        return {"product_id": pid, "product_name": p.get("product_name") or f"상품 {pid}",
                "sale_period": p.get("sale_period"), "sale": sale_state(p.get("sale_period"), today),
                "subsidy": p.get("government_subsidy"), "deductible": p.get("deductible"),
                "contacts": p.get("contacts"), "has_coverage_data": bool(covs.get(pid)),
                "heat_main": [x["name"] for x in prof["main"]], "heat_riders": [x["name"] for x in prof["riders"]],
                "heat_excl": [x["name"] for x in prof["excl"]], "peril": prof["peril"]}

    cards = []
    for sp in species:
        entries = by_mapping.get(sp["mapping_id"], [])
        chosen, state = pick_contract(entries, today)
        risk = species_risk(sp, sea, today)
        card: dict[str, Any] = {
            "mapping_id": sp["mapping_id"], "species_id": sp["species_id"], "species": species_label(sp),
            "standard_name": sp["standard_name"], "category": sp["category"], "method": sp["farming_method"],
            "life_stage": sp["life_stage"], "is_primary": sp["is_primary"], "production_ratio": sp["production_ratio"],
            "species_status": sp["status"], "risk": risk,
            "contract": None, "product": None, "heat": None, "recommend": None, "quote": None,
            "history": [{"insurance_id": c["insurance_id"], "product_name": c.get("product_name"), "status": c["status"],
                         "start_date": c["start_date"], "end_date": c["end_date"]}
                        for c, _ in entries if not chosen or c["insurance_id"] != chosen[0]["insurance_id"]],
        }

        if chosen and state in ("active", "pending"):
            c, t = chosen
            prof = profiles.get(c["product_id"]) or heat_profile([])
            code, heat = heat_status(prof, t.get("riders") or [], M()["perils"], M()["word"])
            if state == "pending":
                code = "pending"
                heat = {**heat, "text": "청약 중 · " + heat["text"]}
            if not covs.get(c["product_id"]):
                heat = {"ok": None, "via": None, "text": "상품 보장 정보(insurance_coverages)가 없어 확인 필요"}
                code = "unknown" if state == "active" else code
            end = _d(c["end_date"])
            card["contract"] = {
                "insurance_id": c["insurance_id"], "policy_number": c.get("policy_number"),
                "product_id": c["product_id"], "product_name": c.get("product_name"),
                "status": c["status"], "start_date": c["start_date"], "end_date": c["end_date"],
                "days_left": (end - today).days if end else None,
                "coverage_amount": t.get("coverage_amount"), "plan": t.get("plan"),
                "deductible_rate": t.get("deductible_rate"), "min_deductible_rate": t.get("min_deductible_rate"),
                "riders": t.get("riders") or [], "installment": t.get("installment"),
                "premium": c.get("premium"), "calc_supported": c.get("calc_supported"),
                "warnings": t.get("warnings") or [],
            }
            card["product"] = product_info(c["product_id"])
            card["heat"] = {**heat, "peril": prof["peril"]}
            card["ins"] = code
        else:
            # 미가입 / 만료 → 추천 상품: 만료 계약 상품 → 주계약 고수온 → 특약 고수온 → 그 외
            prev_pid = chosen[0]["product_id"] if chosen else None
            pids = cand.get(sp["species_id"], [])
            if prev_pid and prev_pid not in pids:
                pids = [prev_pid] + pids

            def rank(pid: int) -> tuple:
                pr = profiles.get(pid) or heat_profile([])
                sale = sale_state(products.get(pid, {}).get("sale_period"), today)["code"]
                return (pid != prev_pid, not pr["main"], not pr["riders"], sale in ("later", "closed"), pid)
            pids = sorted(set(pids), key=rank)
            card["product"] = product_info(pids[0]) if pids else None
            card["alternatives"] = [product_info(p) for p in pids[1:3]]
            if card["product"]:
                prof = profiles.get(card["product"]["product_id"]) or heat_profile([])
                card["heat"] = {**heat_offer_text(prof, M()["word"]), "peril": prof["peril"]}
            card["ins"] = "expired" if state == "expired" else ("none" if pids else "unknown")
            if state == "expired":
                c, t = chosen
                card["expired"] = {"insurance_id": c["insurance_id"], "product_name": c.get("product_name"),
                                   "status": c["status"], "end_date": c["end_date"],
                                   "coverage_amount": t.get("coverage_amount")}
        card["ins_label"] = tx(INS[card["ins"]])
        card["scenarios"] = heat_scenarios(card, today, hh)
        card["recommend"] = tx(recommend(card, sea, today))
        card["mode"] = M()["id"]
        cards.append(card)

    cards.sort(key=lambda c: (URGENCY[c["ins"]], -(c["risk"]["stage"]), not c["is_primary"],
                              -float(c["production_ratio"] or 0)))
    return cards, source


def recommend(card: dict, sea: dict, today: date) -> dict:
    """어종(계약) 하나에 대한 보험 추천 한 줄 + 할 일."""
    code, p, alert = card["ins"], card.get("product") or {}, sea.get("alert")
    pname = p.get("product_name") or "해당 어종 보험"
    sale = p.get("sale") or {"code": "unknown", "text": "판매기간 확인 필요"}
    rider = (p.get("heat_riders") or [None])[0]
    actions: list[str] = []
    if code in ("none", "expired"):
        if not p:
            return {"level": "check", "title": "가입 가능한 상품이 매핑되지 않았어요",
                    "actions": ["insurance_map 에 이 어종이 있는지 확인", "수협에 가입 가능 상품 문의"]}
        head = "재가입 권유" if code == "expired" else "가입 권유"
        if alert:
            title = f"{alert.get('type', '특보')} 발령 중 → 이번 피해는 보상 불가, 다음 판매기간 {head}"
        elif sale["code"] == "later":
            title = f"{pname} {sale['text']} 판매 → 시작 주 상담 예약"
        elif sale["code"] == "closed":
            title = f"{pname} 이번 판매 종료 → 다음 판매기간 안내"
        else:
            title = f"{pname} {head}" + (" (이번 달 마감)" if sale["code"] == "soon" else "")
        if p.get("heat_main"):
            actions.append("고수온이 주계약에 포함된 상품 · 부담보 특약 없이 가입 권유")
        elif rider:
            actions.append(f"{rider} 함께 가입 권유")
        else:
            actions.append("이 상품은 고수온 보장이 없어요 · 고수온 대비 다른 방법(지원사업 등) 안내")
        actions.append("입식 후 20일 안 입식신고 여부 확인")
        return {"level": "high", "title": title, "actions": actions}
    if code == "norider":
        return {"level": "high", "title": f"{rider or '고수온 특약'} 추가 권유",
                "actions": ["보험기간 중 특약 추가 가능 여부를 수협에 확인", "추가 시 보험료·자기부담 안내"]}
    if code == "excl":
        return {"level": "mid", "title": "고수온 부담보 특약 가입 상태 → 다음 갱신 때 고수온 보장 포함 권유",
                "actions": ["증권에서 부담보 특약 확인", "고수온 피해는 이번 계약으로 보상 안 됨을 안내"]}
    if code == "noheat":
        return {"level": "mid", "title": "가입 상품에 고수온 보장이 없어요",
                "actions": ["같은 어종의 고수온 보장 상품·특약이 있는지 수협 확인"]}
    if code == "pending":
        return {"level": "mid", "title": "청약 중 → 보장 개시일·첫 보험료 납입 확인",
                "actions": ["첫 보험료 납입 전 사고는 보상되지 않아요", "특보 전에 보장이 시작되는지 확인"]}
    if code == "unknown":
        return {"level": "check", "title": "보장 내용 확인 필요", "actions": ["증권·상품 보장 정보 확인"]}
    # full
    c = card.get("contract") or {}
    if c.get("days_left") is not None and c["days_left"] <= RENEW_NOTICE_DAYS:
        actions.append(f"만기 {c['end_date']} (D-{c['days_left']}) → 갱신 안내")
    actions.append("경보 시 먹이 주기 중단 · 선별·분조 금지 (약관상 의무)")
    actions.append("피해가 보이면 바로 수협에 알리기")
    return {"level": "low", "title": "고수온 보장 가입 완료 → 대응 의무 안내", "actions": actions}


# ---------------------------------------------------------------------------
# 5. 상담 시 확인 사항 · 예시 멘트 (대시보드 talkingPoints / checkGuide / exampleScript 를 서버로)
# ---------------------------------------------------------------------------
def talking_points(cards: list[dict], sea: dict, stage: int) -> list[dict]:
    if not cards:
        return [{"key": True, "t": "등록된 양식 어종이 없어요 · 어종·입식 현황부터 확인"}]
    L, pts = cards[0], []
    alert = sea.get("alert")
    sale = (L.get("product") or {}).get("sale") or {"code": "unknown"}
    if L["ins"] in ("none", "expired", "unknown", "norider"):
        if alert:
            pts.append({"key": True, "t": f"{alert.get('type', '특보')}가 이미 발령({alert.get('since', '')})돼서 지금 가입해도 "
                                          "이번 고수온 피해는 보상되지 않아요 → 대응 요령 안내 후 다음 판매 기간 가입을 권해요"})
        elif sale["code"] not in ("later", "closed"):
            pts.append({"key": True, "t": "첫 보험료를 낸 날 이미 특보가 난 지역은 그 재해를 보상하지 않아요 → 특보 전에 가입해야 해요"})
    if L["ins"] == "unknown":
        pts.insert(0, {"key": True, "t": "가입 여부·보장 내용부터 확인 · 다른 조합에서 가입했을 수도 있어요"})
    if L["ins"] == "expired":
        e = L.get("expired") or {}
        pts.append({"key": True, "t": f"{e.get('product_name') or '이전'} 계약이 {e.get('end_date')}에 끝났어요 → 재가입 의향 확인"})
    if L["ins"] in ("none", "expired", "unknown"):
        pts.append({"t": "입식 후 20일 안에 품종·수량을 신고했는지 확인 (안 하면 재난지원금·양식보험 가입 불가)", "need": True})
    heat = L.get("heat") or {}
    if heat.get("via") == "특약" and L["ins"] != "full":
        pts.append({"t": "고수온은 기본 보험만으로는 보장되지 않아요 · 특약을 함께 넣어야 해요"})
    if L["ins"] == "excl":
        pts.append({"key": True, "t": "보험료를 줄이려고 고수온을 빼는 특약(부담보)을 넣었는지 증권으로 확인"})
    if L["ins"] == "noheat":
        pts.append({"key": True, "t": "가입한 상품에 고수온 보장이 없어요 · 다른 상품·특약 가능 여부 수협 확인"})
    if L["ins"] == "pending":
        pts.append({"key": True, "t": "청약 상태예요 · 첫 보험료 납입·보장 개시일 확인 (개시 전 사고는 보상 안 됨)"})
    if sale.get("code") == "soon" and L["ins"] in GAP_CODES:
        pts.append({"key": True, "t": f"이 상품은 {sale.get('text')}이에요"})
    if sale.get("code") == "later" and L["ins"] in GAP_CODES:
        pts.append({"t": f"{sale.get('text')} 판매가 시작돼요 · 시작 주에 바로 연락하도록 예약"})
    c = L.get("contract") or {}
    if L["ins"] == "full" and c.get("days_left") is not None and c["days_left"] <= RENEW_NOTICE_DAYS:
        pts.append({"key": True, "t": f"만기 {c['end_date']} (D-{c['days_left']}) · 갱신 안내"})
    if L["ins"] == "full" or stage >= 2:
        pts.append({"t": "경보가 나면 먹이 주기 중단, 선별·분조 금지 (약관상 의무)"})
    if stage >= 2 or alert:
        pts.append({"t": "액화산소 등 대응 장비 점검 안내", "need": True})
    for x in cards[1:]:
        nm = x["standard_name"]
        if x["ins"] == "none":
            s = ((x.get("product") or {}).get("sale") or {})
            pts.append({"key": True, "t": f"{nm}: 미가입 · " + (f"{s.get('text')} 가입 가능" if s.get("code") == "later" else "지금 가입 상담")})
        elif x["ins"] == "expired":
            pts.append({"key": True, "t": f"{nm}: 계약 만료 → 재가입 안내"})
        elif x["ins"] == "norider":
            pts.append({"key": True, "t": f"{nm}: 기본만 가입 → 고수온 특약 추가 안내"})
        elif x["ins"] == "unknown":
            pts.append({"key": True, "t": f"{nm}: 보험 가입·보장 여부 확인"})
        elif x["ins"] == "excl":
            pts.append({"t": f"{nm}: 고수온을 빼고 가입했는지 증권 확인"})
        elif x["ins"] == "pending":
            pts.append({"t": f"{nm}: 청약 중 · 보장 개시일 확인"})
    return pts


def check_guide(L: Optional[dict]) -> str:
    if not L:
        return "양식 어종과 입식 현황부터 확인하세요."
    sale = (L.get("product") or {}).get("sale") or {}
    return {
        "none": (f"판매 시작({sale.get('text')}) 시기를 안내하고, 시작 주에 상담을 예약하세요." if sale.get("code") == "later"
                 else "특보 발령 전, 판매 기한과 인수 조건을 확인하고 가입 의향을 물어보세요."),
        "expired": "지난 계약이 끝났어요. 재가입 의향과 판매 기한을 확인하세요.",
        "norider": "가입한 보험에 고수온 특약이 없어요. 특약을 더할 수 있는지와 가입 의향을 확인하세요.",
        "excl": "증권에서 고수온을 뺀 특약(부담보)이 있는지 확인하세요.",
        "noheat": "가입 상품에 고수온 보장이 없어요. 보완할 수 있는 상품이 있는지 확인하세요.",
        "pending": "청약 중이에요. 첫 보험료 납입과 보장 개시일을 확인하세요.",
        "unknown": "보험 가입 여부와 입식신고 여부부터 확인하세요.",
    }.get(L["ins"], "경보 때 지켜야 할 의무를 안내하고, 피해가 보이면 바로 알리도록 하세요.")


def example_script(owner: str, region: str, L: Optional[dict], sea: dict) -> str:
    """예시 멘트. '{burden}' 자리는 화면에서 견적 합계(예상 부담)로 바뀝니다."""
    st, alert = sea["stage"], sea.get("alert")
    name = owner or "어가"
    lvl = st.get("level") or 0
    first28 = next((d for d in sea["roms"] if d["value"] is not None and reach(d["value"], M()["caution"])), None)
    when = lambda n: "오늘" if n == 0 else f"{n}일 뒤"
    if lvl >= 3 and first28:
        n = (first28["day"] - date.fromisoformat(str(sea["roms"][0]["day"]))).days
        opening = f"“{name}님, {when(n)}부터 {region} 바다가 {M()['caution']:g}℃ 넘는 날이 {ALERT_STREAK_DAYS}일 이상 이어질 것으로"
    elif lvl >= 2 and st.get("temp") is not None:
        opening = f"“{name}님, {when(st['d'])} {region} 바다가 {st['temp']:.1f}℃까지 오를 것으로"
    else:
        opening = f"“{name}님, 앞으로 수온이 오를 것으로"
    opening += " 보여 먼저 연락드렸습니다."
    if not L:
        return f"{opening} 지금 키우시는 어종과 입식 현황을 여쭤봐도 될까요?”"
    sale = (L.get("product") or {}).get("sale") or {}
    pname = (L.get("product") or {}).get("product_name") or "양식보험"
    if alert and L["ins"] in ("none", "expired", "unknown", "norider"):
        return (f"“{name}님, {region}에 {alert.get('type', '고수온 특보')}가 나서 연락드렸습니다. 지금 가입하셔도 이번 피해는 "
                "보상받기 어려워요. 대신 지금 하실 대응을 먼저 알려드리고, 다음 판매 기간에 미리 연락드려도 될까요?”")
    if L["ins"] in ("none", "expired") and sale.get("code") == "later":
        return f"{opening} {pname}은 {sale.get('text')} 가입할 수 있어요. 판매가 시작되면 바로 안내드려도 될까요?”"
    if L["ins"] in ("none", "expired"):
        return f"{opening} 고수온 보험 가입 시 지원을 제외한 예상 부담은 {{burden}}입니다. 조건을 함께 확인해 드릴까요?”"
    if L["ins"] == "norider":
        return f"{opening} 지금 보험에는 고수온이 빠져 있어요. 고수온 특약을 더하면 지원을 제외한 예상 부담은 {{burden}}입니다. 조건을 함께 확인해 드릴까요?”"
    if L["ins"] == "excl":
        return f"{opening} 가입하신 보험에서 고수온을 빼셨는지 증권으로 함께 확인해 드릴까요?”"
    if L["ins"] in ("unknown", "noheat"):
        return f"{opening} 지금 가입하신 보험에 고수온 피해가 보장되는지 함께 확인해 드릴까요?”"
    if L["ins"] == "pending":
        return f"{opening} 청약하신 보험은 첫 보험료를 내신 뒤부터 보장돼요. 납입 일정 확인해 드릴까요?”"
    return f"{opening} 경보가 나면 먹이 주기를 멈추고 선별·분조는 미뤄 주셔야 해요.”"


# ---------------------------------------------------------------------------
# 6. 상담 기록
# ---------------------------------------------------------------------------
CONSULT_STATUS = ("미연락", "부재·재연락", "문자 발송", "상담 예약", "가입 완료", "안내 완료", "보류·거절")
ITEM_RESULT = ("가입", "보류", "안내", "타 기관 가입")


_call_records_ready: Optional[bool] = None


def call_records_ready() -> bool:
    """통화 녹음 테이블(consult_call_records)이 있는지. 마이그레이션 전이면 STT 정보 없이 상담 기록만 보여 줘요."""
    global _call_records_ready
    if not _call_records_ready:   # 없으면 매번 다시 확인 (마이그레이션 후 서버 재시작 없이 반영)
        row = q_one("SELECT to_regclass('customer_service.consult_call_records') IS NOT NULL AS ok")
        _call_records_ready = bool(row and row["ok"])
    return _call_records_ready


def fmt_dur(sec: Any) -> Optional[str]:
    if sec is None:
        return None
    s = int(float(sec))
    return f"{s // 60}:{s % 60:02d}"


def load_consult(household_id: int, today: date) -> dict:
    season = consult_season(today)
    case = q_one("""SELECT case_id, season, status, next_contact_at, last_contact_at
                    FROM customer_service.consult_cases WHERE household_id = %s AND season = %s""",
                 (household_id, season))
    stt = call_records_ready()
    logs = q_all(f"""SELECT l.log_id, l.contacted_at, l.channel, l.result_status, l.memo, l.next_contact_at, l.staff_name,
                           COALESCE(json_agg(json_build_object('mapping_id', i.mapping_id, 'result', i.result,
                                    'species', fs.standard_name)) FILTER (WHERE i.log_item_id IS NOT NULL), '[]') AS items
                           {", cr.stt_status, cr.duration_sec, cr.summary AS stt_summary, cr.error AS stt_error" if stt else ""}
                    FROM customer_service.consult_logs l
                    LEFT JOIN customer_service.consult_log_items i ON i.log_id = l.log_id
                    LEFT JOIN customer_service.household_species hs ON hs.mapping_id = i.mapping_id
                    LEFT JOIN customer_service.fish_species fs ON fs.species_id = hs.species_id
                    {"LEFT JOIN customer_service.consult_call_records cr ON cr.log_id = l.log_id" if stt else ""}
                    WHERE l.household_id = %s
                    GROUP BY l.log_id{", cr.log_id" if stt else ""}
                    ORDER BY l.contacted_at DESC
                    LIMIT 20""", (household_id,))
    # 통화 녹음 STT: 기록마다 stt = {status: 대기·변환 중·완료·실패, dur: 'm:ss', summary, error} 또는 null
    for lg in logs:
        status = lg.pop("stt_status", None)
        dur, summary, error = lg.pop("duration_sec", None), lg.pop("stt_summary", None), lg.pop("stt_error", None)
        lg["stt"] = None if status is None else {
            "status": status, "dur": fmt_dur(dur), "summary": summary,
            "error": error if status == "실패" else None}
    return {"season": season, "status": case["status"] if case else "미연락",
            "next_contact_at": case["next_contact_at"] if case else None,
            "last_contact_at": case["last_contact_at"] if case else None, "logs": logs}


class ConsultItem(BaseModel):
    mapping_id: int
    result: Literal["가입", "보류", "안내", "타 기관 가입"]
    insurance_id: Optional[int] = None


class ConsultLogIn(BaseModel):
    result_status: Literal["부재·재연락", "문자 발송", "상담 예약", "가입 완료", "안내 완료", "보류·거절"]
    channel: Literal["전화", "문자", "방문", "창구"] = "전화"
    next_contact_at: Optional[datetime] = None
    memo: Optional[str] = Field(None, max_length=2000)
    staff_name: Optional[str] = Field(None, max_length=50)
    items: list[ConsultItem] = []


# ---------------------------------------------------------------------------
# 7. API
# ---------------------------------------------------------------------------
router = APIRouter(tags=["어가 상세"])


def ok(obj: Any) -> JSONResponse:
    return JSONResponse(content=to_json(obj))


@router.get("/api/households/health")
def health():
    return {"ok": True, "claim_api": claim_api.mode, "claim_api_detail": claim_api.status_detail}


@router.get("/api/households")
def list_households(q: Optional[str] = Query(None, description="어가명·대표자·면허번호·어종 일부"),
                    limit: int = Query(200, ge=1, le=1000)):
    """상세 화면의 어가 선택 목록. 보험 요약은 유효 계약에 걸린 어종 수 기준."""
    today = today_kst()
    rows = q_all("""
        SELECT h.household_id, h.household_name, h.owner_name, s.sigungu_name, s.area_name,
               COALESCE(array_agg(DISTINCT fs.standard_name) FILTER (WHERE fs.species_id IS NOT NULL), '{}') AS species,
               count(DISTINCT hs.mapping_id) AS n_species,
               count(DISTINCT t.mapping_id) FILTER (WHERE c.contract_status = '유효'
                                                      AND %(today)s BETWEEN c.start_date AND c.end_date) AS n_insured
        FROM customer_service.fishery_households h
        LEFT JOIN public.sigungu_area_coordinates s ON s.region_id = h.region_code
        LEFT JOIN customer_service.household_species hs ON hs.household_id = h.household_id
        LEFT JOIN customer_service.fish_species fs ON fs.species_id = hs.species_id
        LEFT JOIN customer_service.insurance_targets t ON t.mapping_id = hs.mapping_id
        LEFT JOIN customer_service.insurance_contracts c ON c.insurance_id = t.insurance_id
        WHERE %(q)s::text IS NULL
           OR h.household_name ILIKE '%%' || %(q)s || '%%' OR h.owner_name ILIKE '%%' || %(q)s || '%%'
           OR h.license_number ILIKE '%%' || %(q)s || '%%' OR fs.standard_name ILIKE '%%' || %(q)s || '%%'
        GROUP BY h.household_id, s.sigungu_name, s.area_name
        ORDER BY h.household_id
        LIMIT %(limit)s""", {"q": q or None, "today": today, "limit": limit})
    return ok([{**r, "region": normalize_sigungu(r["sigungu_name"]),
                "insurance": "none" if r["n_insured"] == 0 else "partial" if r["n_insured"] < r["n_species"] else "insured"}
               for r in rows])


@router.get("/api/households/{household_id}/detail")
@cache_detail_request
def household_detail(household_id: int,
                     mode: Literal["heat", "cold"] = Query("heat", description="heat = 고수온 · cold = 저수온 기준")):
    token = _MODE.set(mode)
    try:
        return _household_detail(household_id)
    finally:
        _MODE.reset(token)


def _household_detail(household_id: int):
    today = today_kst()
    hh = q_one(HOUSEHOLD_SQL, (household_id,))
    if hh is None:
        raise HTTPException(404, "어가 정보를 찾을 수 없어요")
    species = [s for s in q_all(SPECIES_SQL, (household_id,)) if s["status"] != "폐업"]

    sea = build_sea(hh, today)
    cards, ins_source = build_cards(household_id, species, sea, today, hh)
    region_stage = sea["stage"]["level"] or 0
    for c in cards:
        c["stage"] = max(region_stage, c["risk"]["stage"])
    stage = max([region_stage] + [c["stage"] for c in cards])
    lead = cards[0] if cards else None
    region = normalize_sigungu(hh["sigungu_name"]) or sea["region"] or "-"

    return ok({
        "as_of": datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        "household": {
            "household_id": hh["household_id"], "household_name": hh["household_name"], "owner_name": hh["owner_name"],
            "fishery_type": hh["fishery_type"], "license_number": hh["license_number"],
            "sigungu": hh["sigungu_name"], "area": hh["area_name"], "region": region,
            "lat": hh["lat"], "lon": hh["lon"],
            "phone": hh["phone_number"], "phone_masked": mask_phone(hh["phone_number"]),
        },
        "sea": sea,
        "stage": {"level": stage, "label": STAGE[stage]},
        "cards": cards,
        "insurance_source": ins_source,          # claim_api | db
        "gap_count": sum(1 for c in cards if c["ins"] in GAP_CODES),
        "mode": M()["id"],
        "guide": tx(check_guide(lead)),
        "checks": tx(talking_points(cards, sea, stage)),
        "script": tx(example_script(hh["owner_name"], region, lead, sea)),
        "consult": load_consult(household_id, today),
    })


@router.post("/api/households/{household_id}/consult-logs")
def add_consult_log(household_id: int, body: ConsultLogIn):
    """상담 결과 저장: consult_cases(시즌별 1건) 상태 갱신 + consult_logs + consult_log_items."""
    today = today_kst()
    nxt = body.next_contact_at
    if body.result_status == "부재·재연락" and nxt is None:
        nxt = datetime.combine(today + timedelta(days=1), datetime.min.time(), tzinfo=KST).replace(hour=10)
    if body.result_status == "상담 예약" and nxt is None:
        raise HTTPException(422, "상담 예약은 다음 연락·상담일이 필요해요")
    if nxt is not None and nxt.tzinfo is None:
        nxt = nxt.replace(tzinfo=KST)
    with db().connection() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT 1 FROM customer_service.fishery_households WHERE household_id = %s", (household_id,))
        if cur.fetchone() is None:
            raise HTTPException(404, "어가 정보를 찾을 수 없어요")
        if body.items:
            # consult_log_items 는 (log_id, mapping_id) UNIQUE → 한 번에 같은 어종을 두 번 보내면 안 됨
            ids = [i.mapping_id for i in body.items]
            dup = sorted({m for m in ids if ids.count(m) > 1})
            if dup:
                raise HTTPException(422, f"같은 어종 결과가 두 번 들어왔어요: mapping_id {dup}")
            cur.execute("SELECT mapping_id FROM customer_service.household_species WHERE household_id = %s", (household_id,))
            own = {r["mapping_id"] for r in cur.fetchall()}
            bad = [i.mapping_id for i in body.items if i.mapping_id not in own]
            if bad:
                raise HTTPException(422, f"이 어가의 어종이 아니에요: mapping_id {bad}")
            # insurance_id 는 이 어가 계약이면서 그 어종(mapping_id)이 보험목적으로 걸린 계약이어야 함
            ins_pairs = [(i.insurance_id, i.mapping_id) for i in body.items if i.insurance_id is not None]
            if ins_pairs:
                cur.execute("""SELECT t.insurance_id, t.mapping_id
                               FROM customer_service.insurance_targets t
                               JOIN customer_service.insurance_contracts c ON c.insurance_id = t.insurance_id
                               WHERE c.household_id = %s""", (household_id,))
                valid = {(r["insurance_id"], r["mapping_id"]) for r in cur.fetchall()}
                bad_ins = [p for p in ins_pairs if p not in valid]
                if bad_ins:
                    raise HTTPException(422, f"이 어가·어종의 계약이 아니에요: (insurance_id, mapping_id) {bad_ins}")
        cur.execute("""
            INSERT INTO customer_service.consult_cases (household_id, season, status, next_contact_at, last_contact_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (household_id, season) DO UPDATE
               SET status = EXCLUDED.status, next_contact_at = EXCLUDED.next_contact_at,
                   last_contact_at = now(), updated_at = now()
            RETURNING case_id""", (household_id, consult_season(today), body.result_status, nxt))
        case_id = cur.fetchone()["case_id"]
        cur.execute("""
            INSERT INTO customer_service.consult_logs
                   (case_id, household_id, channel, result_status, memo, next_contact_at, staff_name)
            VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING log_id""",
                    (case_id, household_id, body.channel, body.result_status, body.memo, nxt, body.staff_name))
        log_id = cur.fetchone()["log_id"]
        for it in body.items:
            cur.execute("""INSERT INTO customer_service.consult_log_items (log_id, mapping_id, result, insurance_id)
                           VALUES (%s, %s, %s, %s)""", (log_id, it.mapping_id, it.result, it.insurance_id))
    return ok({"log_id": log_id, "case_id": case_id, "consult": load_consult(household_id, today)})


class EstimateIn(BaseModel):
    """고수온 피해 시 예상 보험금 (상담용 간이 입력). 단위는 적용가격 단위에 맞춰 마리 또는 kg."""
    insurance_id: int
    mapping_id: int
    accident_date: Optional[date] = None          # 비우면 오늘
    peril: Optional[str] = None                   # 비우면 상품의 고수온 재해명(고수온/이상수온)
    held_count: Optional[Decimal] = None
    damaged_count: Optional[Decimal] = None
    held_kg: Optional[Decimal] = None
    damaged_kg: Optional[Decimal] = None
    avg_weight_g: Optional[Decimal] = None
    price_variant: Optional[str] = None


@router.get("/api/households/{household_id}/consult")
def household_consult(household_id: int):
    """상담 기록만 (통화 녹음을 올린 뒤 STT 결과를 다시 받을 때). 수온·보험 계산 없이 가벼워요."""
    if q_one("SELECT 1 FROM customer_service.fishery_households WHERE household_id = %s", (household_id,)) is None:
        raise HTTPException(404, "어가 정보를 찾을 수 없어요")
    return ok(load_consult(household_id, today_kst()))


@router.post("/api/households/{household_id}/claim-estimate")
def claim_estimate(household_id: int, body: EstimateIn):
    """보험금 계산 API(/claims/estimate)에 그대로 넘깁니다. 계약이 이 어가 것인지 먼저 확인."""
    row = q_one("""SELECT c.product_id FROM customer_service.insurance_contracts c
                   JOIN customer_service.insurance_targets t ON t.insurance_id = c.insurance_id
                   WHERE c.household_id = %s AND c.insurance_id = %s AND t.mapping_id = %s""",
                (household_id, body.insurance_id, body.mapping_id))
    if row is None:
        raise HTTPException(404, "이 어가의 계약·어종이 아니에요")
    peril = body.peril
    if not peril:
        prof = heat_profile(q_all(COVERAGES_SQL, ([row["product_id"]],)))
        peril = prof["peril"]
    aquatic = {k: v for k, v in body.model_dump().items()
               if k in ("held_count", "damaged_count", "held_kg", "damaged_kg", "avg_weight_g", "price_variant") and v is not None}
    payload = {"insurance_id": body.insurance_id, "mapping_id": body.mapping_id,
               "accident_date": body.accident_date or today_kst(), "peril": peril, "aquatic": aquatic or None}
    try:
        return ok(claim_api.estimate(payload))
    except httpx.HTTPError as e:
        return JSONResponse(status_code=502, content={"detail": f"보험금 계산 API 호출 실패: {type(e).__name__}"})
    except ClaimApiError as e:
        detail = e.detail if isinstance(e.detail, dict) else {"detail": e.detail}
        return JSONResponse(status_code=e.status, content=to_json(detail))
