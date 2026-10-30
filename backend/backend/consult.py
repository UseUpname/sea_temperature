"""Consultation dashboard API: customer list, consultation history, saving a consultation,
call recordings (STT + AI summary, see consult_stt.py) and per-customer memos.

Coverage is derived from contracts and riders, never stored.
No staff authentication exists yet: run only on a trusted local network.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path as FilePath
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, HTTPException, Path, Query, UploadFile
from fastapi.responses import FileResponse
from psycopg import Connection
from pydantic import BaseModel, Field, model_validator

from backend import consult_stt
from backend.customer_api import request_protection
from backend.db import get_customer_db, get_db

SEASON = "2026-고수온"
KST = timezone(timedelta(hours=9))
STATUS = Literal["미연락", "부재·재연락", "문자 발송", "상담 예약", "가입 완료", "안내 완료", "보류·거절"]
# Products whose base policy already covers high temperature (상품요약서 2026.5.13: 해상 전복, 오만둥이).
# Interim: insurance.insurance_products has no coverage-type column yet.
BASE_HEAT_PRODUCTS = [2, 20]
HEAT_RIDER = "고수온원인 수산물손해담보 특별약관"
HEAT_EXCLUSION = "이상수온원인 수산물손해부담보 특별약관"
# 저수온 모드(?mode=cold): 저수온 특약이 있거나, 주계약에 저수온이 들어 있는 상품(insurance_coverages 에서 찾음)이면 보장
COLD_RIDER = "저수온원인 수산물손해담보 특별약관"
COLD_BASE_SQL = """SELECT DISTINCT product_id FROM insurance.insurance_coverages
                   WHERE contract_type = '주계약' AND effect = '보장추가' AND risk_types::text LIKE '%%저수온%%'"""
REGION_SHORT = "regexp_replace(r.sigungu_name, '(시|군)$', '')"


def mask_phone(phone: str | None) -> str | None:
    return None if phone is None else f"{phone[:3]}-****-{phone[-4:]}"


def contracts_by_household(db: Connection, household_ids: list[int], mode: str = "heat") -> dict[int, list[dict]]:
    if mode == "cold":
        rider, riders_short = COLD_RIDER, "저수온"
        base = [r["product_id"] for r in db.execute(COLD_BASE_SQL).fetchall()]
    else:
        rider, riders_short, base = HEAT_RIDER, HEAT_RIDER, BASE_HEAT_PRODUCTS
    rows = db.execute("""
        SELECT m.household_id, m.mapping_id, s.standard_name AS species,
               CASE
                 WHEN bool_or(c.insurance_id IS NOT NULL AND (
                        COALESCE(t.rider_details->'riders' ?| ARRAY[%(rider)s, %(short)s], false)
                        OR (c.product_id = ANY(%(base)s)
                            AND NOT COALESCE(t.rider_details->'riders' ? %(exclusion)s, false))))
                   THEN 'covered'
                 WHEN bool_or(c.insurance_id IS NOT NULL) THEN 'no_rider'
                 ELSE 'none'
               END AS coverage
        FROM customer_service.household_species m
        JOIN customer_service.fish_species s ON s.species_id = m.species_id
        LEFT JOIN customer_service.insurance_targets t ON t.mapping_id = m.mapping_id
        -- Only valid aquatic-product contracts for this species count; facility policies do not.
        LEFT JOIN customer_service.insurance_contracts c
               ON c.insurance_id = t.insurance_id AND c.contract_status = '유효'
              AND EXISTS (SELECT 1 FROM insurance.insurance_map im
                          WHERE im.product_id = c.product_id AND im.species_id = m.species_id
                            AND im.coverage_target = '수산물')
        WHERE m.household_id = ANY(%(ids)s) AND m.status <> '폐업'
        GROUP BY m.household_id, m.mapping_id, s.standard_name, m.is_primary
        ORDER BY m.household_id, m.is_primary DESC, m.mapping_id
    """, {"rider": rider, "short": riders_short, "base": base, "exclusion": HEAT_EXCLUSION,
          "ids": household_ids}).fetchall()
    grouped: dict[int, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["household_id"], []).append(
            {"mapping_id": row["mapping_id"], "species": row["species"],
             "coverage": row["coverage"], "sale_text": None})
    return grouped


def list_customers(db: Connection, season: str, mode: str = "heat") -> list[dict]:
    rows = db.execute(f"""
        SELECT h.household_id, h.owner_name AS name, h.phone_number, h.sms_consent,
               {REGION_SHORT} AS region, COALESCE(cs.status, '미연락') AS status,
               lg.last_contact, COALESCE(lg.consult_count, 0) AS consult_count, q.risk_stage,
               COALESCE(mc.memo_count, 0) AS memo_count
        FROM customer_service.fishery_households h
        LEFT JOIN public.sigungu_area_coordinates r ON r.region_id = h.region_code
        LEFT JOIN customer_service.consult_cases cs ON cs.household_id = h.household_id AND cs.season = %s
        LEFT JOIN (SELECT household_id, max(contacted_at AT TIME ZONE 'Asia/Seoul')::date AS last_contact,
                          count(*) AS consult_count
                   FROM customer_service.consult_logs GROUP BY household_id) lg ON lg.household_id = h.household_id
        LEFT JOIN (SELECT household_id, count(*) AS memo_count
                   FROM customer_service.customer_memos GROUP BY household_id) mc ON mc.household_id = h.household_id
        LEFT JOIN LATERAL (SELECT risk_stage FROM customer_service.contact_queue q
                           WHERE q.household_id = h.household_id ORDER BY q.base_date DESC LIMIT 1) q ON true
        ORDER BY h.household_id
    """, (season,)).fetchall()
    contracts = contracts_by_household(db, [r["household_id"] for r in rows], mode)
    return [{"code": str(r["household_id"]), "name": r["name"], "phone_masked": mask_phone(r["phone_number"]),
             "sms_consent": r["sms_consent"], "region": r["region"],
             "contracts": contracts.get(r["household_id"], []), "risk_stage": r["risk_stage"],
             "status": r["status"], "last_contact": r["last_contact"], "consult_count": r["consult_count"],
             "memo_count": r["memo_count"]}
            for r in rows]


def list_history(db: Connection, household_id: int | None = None) -> list[dict]:
    rows = db.execute(f"""
        SELECT l.log_id, l.household_id, h.owner_name AS name, {REGION_SHORT} AS region, l.channel, l.staff_name,
               (l.contacted_at AT TIME ZONE 'Asia/Seoul')::date AS date,
               to_char(l.contacted_at AT TIME ZONE 'Asia/Seoul', 'HH24:MI') AS time, l.result_status, l.memo,
               cr.stt_status, cr.duration_sec,
               ARRAY(SELECT s.standard_name FROM customer_service.household_species m
                     JOIN customer_service.fish_species s ON s.species_id = m.species_id
                     WHERE m.household_id = l.household_id ORDER BY m.is_primary DESC, m.mapping_id) AS species
        FROM customer_service.consult_logs l
        JOIN customer_service.fishery_households h ON h.household_id = l.household_id
        LEFT JOIN public.sigungu_area_coordinates r ON r.region_id = h.region_code
        LEFT JOIN customer_service.consult_call_records cr ON cr.log_id = l.log_id
        WHERE %(hid)s::integer IS NULL OR l.household_id = %(hid)s
        ORDER BY l.contacted_at DESC, l.log_id DESC
    """, {"hid": household_id}).fetchall()
    return [{"log_id": r["log_id"], "code": str(r["household_id"]), "name": r["name"], "region": r["region"],
             "species": r["species"], "date": r["date"], "result": r["result_status"], "memo": r["memo"] or "–",
             "time": r["time"], "channel": r["channel"], "staff_name": r["staff_name"],
             # 통화 기록: has_stt 가 true 면 화면에 '통화 기록 보기' 버튼 (stt_status: 대기 · 변환 중 · 완료 · 실패 · null=녹음 없음)
             "stt_status": r["stt_status"], "has_stt": r["stt_status"] == "완료",
             "stt_dur": consult_stt.fmt_ts(r["duration_sec"]) if r["duration_sec"] is not None else None}
            for r in rows]


def save_consultation(db: Connection, household_id: int, season: str, status: str, channel: str,
                      memo: str | None, next_contact_at, items: list[dict], staff_name: str | None = None) -> int:
    """Upsert the case, append one log and its per-species results; runs in the caller's transaction."""
    if db.execute("SELECT 1 FROM customer_service.fishery_households WHERE household_id = %s",
                  (household_id,)).fetchone() is None:
        raise ValueError("존재하지 않는 어가입니다.")
    mapping_ids = [i["mapping_id"] for i in items]
    owned = db.execute("""
        SELECT count(*) AS n FROM customer_service.household_species
        WHERE household_id = %s AND mapping_id = ANY(%s)
    """, (household_id, mapping_ids)).fetchone()["n"]
    if owned != len(set(mapping_ids)):
        raise ValueError("다른 어가의 어종이 포함되어 있습니다.")
    insurance_ids = [i["insurance_id"] for i in items if i.get("insurance_id") is not None]
    if insurance_ids:
        own_contracts = db.execute("""
            SELECT count(*) AS n FROM customer_service.insurance_contracts
            WHERE household_id = %s AND insurance_id = ANY(%s)
        """, (household_id, insurance_ids)).fetchone()["n"]
        if own_contracts != len(set(insurance_ids)):
            raise ValueError("다른 어가의 보험 계약이 포함되어 있습니다.")
    case_id = db.execute("""
        INSERT INTO customer_service.consult_cases (household_id, season, status, next_contact_at, last_contact_at)
        VALUES (%s, %s, %s, %s, now())
        ON CONFLICT (household_id, season) DO UPDATE
           SET status = EXCLUDED.status, next_contact_at = EXCLUDED.next_contact_at,
               last_contact_at = EXCLUDED.last_contact_at, updated_at = now()
        RETURNING case_id
    """, (household_id, season, status, next_contact_at)).fetchone()["case_id"]
    log_id = db.execute("""
        INSERT INTO customer_service.consult_logs
            (case_id, household_id, channel, result_status, memo, next_contact_at, staff_name)
        VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING log_id
    """, (case_id, household_id, channel, status, memo, next_contact_at, staff_name)).fetchone()["log_id"]
    for item in items:
        db.execute("""
            INSERT INTO customer_service.consult_log_items (log_id, mapping_id, result, insurance_id)
            VALUES (%s, %s, %s, %s)
        """, (log_id, item["mapping_id"], item["result"], item.get("insurance_id")))
    return log_id


def delete_consultation(db: Connection, log_id: int) -> dict:
    """Delete one log and its per-species results, then re-derive the case from the logs that remain.

    consult_cases holds the latest state of a household for a season, so after a deletion it must
    match the newest remaining log; with no logs left the case row is removed (the household reads
    as '미연락' again). Runs in the caller's transaction.
    """
    log = db.execute("""
        SELECT log_id, case_id, household_id FROM customer_service.consult_logs WHERE log_id = %s
    """, (log_id,)).fetchone()
    if log is None:
        raise LookupError("이미 지워졌거나 없는 상담 기록입니다.")
    # Lock the case so a concurrent save or delete for the same household cannot interleave.
    db.execute("SELECT 1 FROM customer_service.consult_cases WHERE case_id = %s FOR UPDATE", (log["case_id"],))
    db.execute("DELETE FROM customer_service.consult_log_items WHERE log_id = %s", (log_id,))
    db.execute("DELETE FROM customer_service.consult_logs WHERE log_id = %s", (log_id,))
    latest = db.execute("""
        SELECT result_status, next_contact_at, contacted_at FROM customer_service.consult_logs
        WHERE case_id = %s ORDER BY contacted_at DESC, log_id DESC LIMIT 1
    """, (log["case_id"],)).fetchone()
    if latest is None:
        db.execute("DELETE FROM customer_service.consult_cases WHERE case_id = %s", (log["case_id"],))
        status = "미연락"
    else:
        db.execute("""
            UPDATE customer_service.consult_cases
               SET status = %s, next_contact_at = %s, last_contact_at = %s, updated_at = now()
             WHERE case_id = %s
        """, (latest["result_status"], latest["next_contact_at"], latest["contacted_at"], log["case_id"]))
        status = latest["result_status"]
    return {"log_id": log_id, "household_id": log["household_id"], "status": status}


# ===== 통화 녹음 · STT =====

def get_log(db: Connection, log_id: int) -> dict:
    log = db.execute("""
        SELECT log_id, household_id, channel, staff_name FROM customer_service.consult_logs WHERE log_id = %s
    """, (log_id,)).fetchone()
    if log is None:
        raise LookupError("없는 상담 기록입니다.")
    return log


def run_stt(db: Connection, log_id: int, audio_path: FilePath, mime: str | None) -> dict:
    """Transcribe + summarize, then upsert the call record. A failure is stored as '실패' (not raised),
    so the screen can show it and offer '다시 변환'."""
    try:
        r = consult_stt.process(audio_path)
        status, error = "완료", None
    except Exception as exc:   # OpenAI/network/file errors
        r, status, error = {}, "실패", f"{type(exc).__name__}: {exc}"[:500]
    db.execute("""
        INSERT INTO customer_service.consult_call_records
            (log_id, audio_path, audio_mime, duration_sec, stt_status, stt_model, summary_model,
             staff_speaker, lines, summary, found, error)
        VALUES (%(log_id)s, %(path)s, %(mime)s, %(dur)s, %(status)s, %(stt)s, %(sum)s,
                %(staff)s, %(lines)s::jsonb, %(summary)s, %(found)s::jsonb, %(error)s)
        ON CONFLICT (log_id) DO UPDATE SET
            audio_path = EXCLUDED.audio_path, audio_mime = EXCLUDED.audio_mime, duration_sec = EXCLUDED.duration_sec,
            stt_status = EXCLUDED.stt_status, stt_model = EXCLUDED.stt_model, summary_model = EXCLUDED.summary_model,
            staff_speaker = EXCLUDED.staff_speaker, lines = EXCLUDED.lines, summary = EXCLUDED.summary,
            found = EXCLUDED.found, error = EXCLUDED.error, updated_at = now()
    """, {"log_id": log_id, "path": str(audio_path), "mime": mime, "dur": r.get("duration_sec"), "status": status,
          "stt": consult_stt.STT_MODEL, "sum": consult_stt.SUMMARY_MODEL, "staff": r.get("staff_speaker"),
          "lines": json.dumps(r["lines"], ensure_ascii=False) if "lines" in r else None,
          "summary": r.get("summary"),
          "found": json.dumps(r["found"], ensure_ascii=False) if "found" in r else None, "error": error})
    return {"log_id": log_id, "status": status, "error": error}


def get_stt(db: Connection, log_id: int) -> dict:
    row = db.execute("""
        SELECT cr.stt_status, cr.duration_sec, cr.lines, cr.summary, cr.found, cr.error, l.staff_name
        FROM customer_service.consult_call_records cr
        JOIN customer_service.consult_logs l ON l.log_id = cr.log_id
        WHERE cr.log_id = %s
    """, (log_id,)).fetchone()
    if row is None:
        raise LookupError("이 상담 기록에는 통화 녹음이 없습니다.")
    return {"status": row["stt_status"], "error": row["error"], "staff_name": row["staff_name"],
            "dur": consult_stt.fmt_ts(row["duration_sec"]) if row["duration_sec"] is not None else None,
            "audio_url": f"/api/consult/logs/{log_id}/audio",
            "summary": row["summary"], "lines": row["lines"] or [], "found": row["found"] or []}


# ===== 고객 참고사항 =====

def memo_row(r: dict) -> dict:
    at = r["created_at"].astimezone(KST) if r["created_at"].tzinfo else r["created_at"]
    return {"memo_id": r["memo_id"], "cat": r["category"], "txt": r["content"], "src": r["source"],
            "log_id": r["log_id"], "staff_name": r["staff_name"], "at": at.strftime("%Y-%m-%d %H:%M")}


def list_memos(db: Connection, household_id: int) -> list[dict]:
    rows = db.execute("""
        SELECT memo_id, category, content, source, log_id, staff_name, created_at
        FROM customer_service.customer_memos WHERE household_id = %s
        ORDER BY created_at DESC, memo_id DESC
    """, (household_id,)).fetchall()
    return [memo_row(r) for r in rows]


def add_memo(db: Connection, household_id: int, cat: str, txt: str, src: str,
             log_id: int | None, staff_name: str | None) -> dict:
    if db.execute("SELECT 1 FROM customer_service.fishery_households WHERE household_id = %s",
                  (household_id,)).fetchone() is None:
        raise ValueError("존재하지 않는 어가입니다.")
    if log_id is not None and db.execute("""
            SELECT 1 FROM customer_service.consult_logs WHERE log_id = %s AND household_id = %s
        """, (log_id, household_id)).fetchone() is None:
        raise ValueError("이 어가의 상담 기록이 아닙니다.")
    # 같은 내용이 이미 있으면 그 참고사항을 그대로 돌려줘요 (두 번 눌러도 하나만)
    row = db.execute("""
        INSERT INTO customer_service.customer_memos (household_id, category, content, source, log_id, staff_name)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (household_id, content) DO UPDATE SET content = EXCLUDED.content
        RETURNING memo_id, category, content, source, log_id, staff_name, created_at
    """, (household_id, cat, txt, src, log_id, staff_name)).fetchone()
    return memo_row(row)


class ConsultItem(BaseModel):
    mapping_id: int = Field(gt=0)
    result: Literal["가입", "보류", "안내", "타 기관 가입"]
    insurance_id: int | None = Field(default=None, gt=0)


class ConsultInput(BaseModel):
    household_id: int = Field(gt=0)
    season: str = Field(default=SEASON, min_length=1, max_length=20)
    status: STATUS
    channel: Literal["전화", "문자", "방문", "창구"]
    memo: str | None = Field(default=None, max_length=1000)
    next_contact_at: datetime | None = None
    staff_name: str | None = Field(default=None, max_length=50)
    items: list[ConsultItem] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def next_contact_required(self):
        # Mirrors the consult_cases CHECK so clients get 422 instead of a generic DB error.
        if self.status in ("부재·재연락", "상담 예약") and self.next_contact_at is None:
            raise ValueError("부재·재연락, 상담 예약은 다음 연락 일시가 필요합니다.")
        return self


class MemoInput(BaseModel):
    cat: Literal["연락", "관심", "경험", "가족", "소통"]
    txt: str = Field(min_length=1, max_length=200)
    src: Literal["stt", "staff"] = "staff"
    log_id: int | None = Field(default=None, gt=0)
    staff_name: str | None = Field(default=None, max_length=50)

    @model_validator(mode="after")
    def clean(self):
        self.txt = self.txt.strip()
        if not self.txt:
            raise ValueError("참고사항 내용을 적어 주세요.")
        if self.src == "stt" and self.log_id is None:
            raise ValueError("통화에서 추가한 참고사항은 상담 기록 번호(log_id)가 필요합니다.")
        return self


router = APIRouter(prefix="/api/consult", tags=["상담 대시보드"], dependencies=[Depends(request_protection)])
ReadDB = Annotated[Connection, Depends(get_db)]
WriteDB = Annotated[Connection, Depends(get_customer_db, scope="function")]


@router.get("/customers")
def customers(db: ReadDB, season: Annotated[str, Query(min_length=1, max_length=20)] = SEASON,
              mode: Literal["heat", "cold"] = "heat"):
    """mode=cold 면 계약별 보장(coverage)을 저수온 기준으로 판정해요."""
    return list_customers(db, season, mode)


@router.get("/history")
def history(db: ReadDB, household_id: Annotated[int | None, Query(gt=0)] = None):
    return list_history(db, household_id)


@router.post("/logs", status_code=201)
def create_log(data: ConsultInput, db: WriteDB):
    try:
        log_id = save_consultation(db, data.household_id, data.season, data.status, data.channel, data.memo,
                                   data.next_contact_at, [i.model_dump() for i in data.items], data.staff_name)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return {"log_id": log_id}


@router.delete("/logs/{log_id}")
def remove_log(log_id: Annotated[int, Path(gt=0)], db: WriteDB):
    """Delete one consultation record (the screen asks the user to confirm first)."""
    rec = db.execute("SELECT audio_path FROM customer_service.consult_call_records WHERE log_id = %s",
                     (log_id,)).fetchone()
    try:
        result = delete_consultation(db, log_id)   # 통화 기록(consult_call_records)은 함께 지워져요
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    if rec:
        FilePath(rec["audio_path"]).unlink(missing_ok=True)
    return result


# ----- 통화 녹음 · STT -----
# 녹음을 올리면 그 자리에서 STT + 요약까지 하고 결과를 돌려줘요 (통화 몇 분이면 수십 초).
# 통화가 많아지면 큐(Celery·RQ 등)로 옮겨 '변환 중' 상태를 쓰면 돼요.

@router.post("/logs/{log_id}/recording")
def upload_recording(log_id: Annotated[int, Path(gt=0)], db: WriteDB, file: UploadFile = File(...)):
    """녹음 파일(mp3 · m4a · wav · webm 등, 25MB 이하)을 이 상담 기록에 붙이고 STT를 돌려요."""
    try:
        get_log(db, log_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    ext = FilePath(file.filename or "").suffix.lower()
    if ext not in consult_stt.AUDIO_TYPES:
        raise HTTPException(422, f"녹음 파일 형식을 확인해 주세요 ({', '.join(sorted(consult_stt.AUDIO_TYPES))}).")
    data = file.file.read(consult_stt.MAX_BYTES + 1)
    if len(data) > consult_stt.MAX_BYTES:
        raise HTTPException(413, "녹음 파일은 25MB 이하만 올릴 수 있어요. 압축하거나 나눠서 올려 주세요.")
    if not data:
        raise HTTPException(422, "빈 파일입니다.")
    consult_stt.AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    path = consult_stt.AUDIO_DIR / f"{log_id}{ext}"
    for old in consult_stt.AUDIO_DIR.glob(f"{log_id}.*"):   # 다시 올리면 이전 녹음은 지워요
        old.unlink(missing_ok=True)
    path.write_bytes(data)
    return run_stt(db, log_id, path, file.content_type)


@router.post("/logs/{log_id}/stt/retry")
def retry_stt(log_id: Annotated[int, Path(gt=0)], db: WriteDB):
    """저장된 녹음으로 STT를 다시 돌려요 ('실패'였을 때)."""
    rec = db.execute("SELECT audio_path, audio_mime FROM customer_service.consult_call_records WHERE log_id = %s",
                     (log_id,)).fetchone()
    if rec is None or not FilePath(rec["audio_path"]).is_file():
        raise HTTPException(404, "다시 변환할 녹음 파일이 없어요. 녹음을 다시 올려 주세요.")
    return run_stt(db, log_id, FilePath(rec["audio_path"]), rec["audio_mime"])


@router.get("/logs/{log_id}/stt")
def stt(log_id: Annotated[int, Path(gt=0)], db: ReadDB):
    """{status, dur, staff_name, audio_url, summary, lines:[["s"|"c","m:ss","말 [[강조]]"]], found:[{cat,txt}]}"""
    try:
        return get_stt(db, log_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None


@router.get("/logs/{log_id}/audio")
def audio(log_id: Annotated[int, Path(gt=0)], db: ReadDB):
    rec = db.execute("SELECT audio_path, audio_mime FROM customer_service.consult_call_records WHERE log_id = %s",
                     (log_id,)).fetchone()
    if rec is None or not FilePath(rec["audio_path"]).is_file():
        raise HTTPException(404, "녹음 파일이 없어요.")
    return FileResponse(rec["audio_path"], media_type=rec["audio_mime"] or None)


# ----- 고객 참고사항 -----

@router.get("/customers/{household_id}/memos")
def memos(household_id: Annotated[int, Path(gt=0)], db: ReadDB):
    return list_memos(db, household_id)


@router.post("/customers/{household_id}/memos", status_code=201)
def create_memo(household_id: Annotated[int, Path(gt=0)], data: MemoInput, db: WriteDB):
    try:
        return add_memo(db, household_id, data.cat, data.txt, data.src, data.log_id, data.staff_name)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


@router.delete("/memos/{memo_id}")
def remove_memo(memo_id: Annotated[int, Path(gt=0)], db: WriteDB):
    row = db.execute("DELETE FROM customer_service.customer_memos WHERE memo_id = %s RETURNING memo_id",
                     (memo_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "이미 지워졌거나 없는 참고사항입니다.")
    return {"memo_id": memo_id}
