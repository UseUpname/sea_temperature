"""Call recording → transcript (OpenAI STT with speaker labels) → summary and memo candidates (OpenAI text model).

Environment:
  OPENAI_API_KEY        required
  OPENAI_STT_MODEL      default gpt-4o-transcribe-diarize (speaker labels + timestamps;
                        OpenAI lists it as deprecated, shutting down 2027-02-26 — swap here when a successor exists)
  OPENAI_SUMMARY_MODEL  default gpt-5-mini
  CONSULT_AUDIO_DIR     where recordings are stored, default data/call_recordings

Recordings contain personal information: send them only for customers who agreed to call recording.
"""
import json
import os
import re
from pathlib import Path

STT_MODEL = os.getenv("OPENAI_STT_MODEL", "gpt-4o-transcribe-diarize")
SUMMARY_MODEL = os.getenv("OPENAI_SUMMARY_MODEL", "gpt-5-mini")
AUDIO_DIR = Path(os.getenv("CONSULT_AUDIO_DIR", "data/call_recordings"))
MAX_BYTES = 25 * 1024 * 1024          # OpenAI audio upload limit
AUDIO_TYPES = {".mp3", ".mp4", ".mpeg", ".mpga", ".m4a", ".wav", ".webm"}
CATEGORIES = ["연락", "관심", "경험", "가족", "소통"]

_client = None


def client():
    """Imported on first use, so the API server still starts when the openai package is missing."""
    global _client
    if _client is None:
        from openai import OpenAI   # pip install openai
        _client = OpenAI()          # reads OPENAI_API_KEY
    return _client


def fmt_ts(sec: float) -> str:
    sec = int(sec or 0)
    return f"{sec // 60}:{sec % 60:02d}"


def transcribe(path: Path) -> list[dict]:
    """Speaker-labelled segments: [{speaker, start, end, text}]."""
    with open(path, "rb") as f:
        tr = client().audio.transcriptions.create(
            model=STT_MODEL, file=f, response_format="diarized_json",
            chunking_strategy="auto",   # required for audio longer than 30 seconds
            language="ko",
        )
    return [{"speaker": s.speaker, "start": float(s.start), "end": float(s.end), "text": s.text.strip()}
            for s in tr.segments if s.text and s.text.strip()]


INSTRUCTIONS = f"""너는 수협 직원과 양식 어민의 보험 상담 통화 기록을 정리한다.
입력은 '[화자 라벨 m:ss] 말' 형식의 STT 기록이다. 기록에 없는 내용은 지어내지 않는다.
- staff_speaker: 수협 직원(상담원)의 화자 라벨. 보통 '수협입니다'처럼 소개하고 보험을 안내하는 쪽이다.
- summary: 상담 결과를 1~2문장, 120자 안으로. '~함', '~하기로 함'처럼 끝낸다. 어종·가입 상태·약속(날짜·시간)을 우선.
- keywords: 기록에 글자 그대로 나오는 중요한 말 3~8개 (예: 보험료, 특약, 미가입, 날짜·시간). 기록에 없는 말은 넣지 않는다.
- found: 다음 상담 때 알아 두면 좋은 이 고객에 대한 사실. 0~5개, 각 40자 안. 분류(cat)는
  연락(통화 가능 시간·약속), 관심(원하는 것·걱정·궁금한 점), 경험(과거 피해·보험 경험), 가족(가족·결정권자), 소통(연락 방법·말투 주의) 중 하나.
  주민등록번호·계좌번호·건강 정보처럼 민감한 정보는 넣지 않는다."""

SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["staff_speaker", "summary", "keywords", "found"],
    "properties": {
        "staff_speaker": {"type": "string"},
        "summary": {"type": "string"},
        "keywords": {"type": "array", "items": {"type": "string"}},
        "found": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["cat", "txt"],
            "properties": {"cat": {"type": "string", "enum": CATEGORIES}, "txt": {"type": "string"}}}},
    },
}


def summarize(segments: list[dict]) -> dict:
    transcript = "\n".join(f"[{s['speaker']} {fmt_ts(s['start'])}] {s['text']}" for s in segments)
    resp = client().responses.create(
        model=SUMMARY_MODEL, instructions=INSTRUCTIONS, input=transcript,
        text={"format": {"type": "json_schema", "name": "consult_call", "schema": SCHEMA, "strict": True}},
    )
    return json.loads(resp.output_text)


def build_lines(segments: list[dict], staff_speaker: str, keywords: list[str]) -> list[list[str]]:
    """[["s"|"c", "m:ss", "말 [[강조]]"]] — the same speaker talking again within 1.5 s is one line."""
    merged: list[dict] = []
    for s in segments:
        if merged and merged[-1]["speaker"] == s["speaker"] and s["start"] - merged[-1]["end"] < 1.5:
            merged[-1]["text"] += " " + s["text"]
            merged[-1]["end"] = s["end"]
        else:
            merged.append(dict(s))
    words = sorted({k.strip() for k in keywords if k and k.strip()}, key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(w) for w in words)) if words else None
    lines = []
    for s in merged:
        text = s["text"].replace("[[", "[").replace("]]", "]")
        if pattern:
            text = pattern.sub(lambda m: f"[[{m.group(0)}]]", text)
        lines.append(["s" if s["speaker"] == staff_speaker else "c", fmt_ts(s["start"]), text])
    return lines


def process(path: Path) -> dict:
    """Run STT + summary for one recording. Raises on OpenAI errors (the caller stores '실패')."""
    segments = transcribe(path)
    if not segments:
        return {"lines": [], "summary": "녹음에서 말소리를 찾지 못했어요.", "found": [],
                "staff_speaker": None, "duration_sec": 0}
    ai = summarize(segments)
    found = [{"cat": f["cat"], "txt": f["txt"].strip()[:200]} for f in ai["found"] if f["txt"].strip()][:5]
    return {"lines": build_lines(segments, ai["staff_speaker"], ai["keywords"]),
            "summary": ai["summary"].strip(), "found": found,
            "staff_speaker": ai["staff_speaker"], "duration_sec": round(max(s["end"] for s in segments), 1)}
