import json
import hashlib
import re
import time
import requests
from google import genai
from google.genai import types
from groq import Groq

SUPABASE_URL = "https://kxtyoopunnwxjhvtxbca.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imt4dHlvb3B1bm53eGpodnR4YmNhIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3ODMyNjg3NSwiZXhwIjoyMDkzOTAyODc1fQ.-KK3F_62XnVI1Fl_VpSK5oI5irMznZu4sdaXkFPZ_f8"
GEMINI_API_KEY = "AIzaSyCfMmaJ7k58qtgwCUZiSo83EHI26VYGTCA"
GROQ_API_KEY = "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"

gemini = genai.Client(api_key=GEMINI_API_KEY)
groq_client = Groq(api_key=GROQ_API_KEY)

TARGET_SEMESTER = "2026-1"
CONFIDENCE_THRESHOLD = 0.5

SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}

SEARCH_PROMPT = """\
{university_name}의 2026년 1학기(봄) 대학 축제 정보를 검색해서 JSON 배열로만 반환해줘.

검색 키워드: {keywords}

추출 규칙:
- date_start, date_end 모두 필수. 종료일 모르면 해당 항목 제외
- 날짜 형식: YYYY-MM-DD. "5월 14일" → "2026-05-14"
- lineup은 실제 공연 아티스트 이름만 (MC/사회자 제외)
- confidence: 날짜+라인업 모두 확실 0.9 / 하나만 0.6 / 불확실 0.3
- 정보 없으면 빈 배열 []

예시:
[{{"festival_name":"아카라카","date_start":"2026-05-21","date_end":"2026-05-22","location":"신촌캠퍼스","lineup":["아이유","에스파"],"confidence":0.9}}]

반환 형식 (JSON 배열만, 마크다운 코드블록 없이):
[{{"festival_name":"...","date_start":"...","date_end":"...","location":"...","lineup":[...],"confidence":0.0}}]
"""

GROQ_PROMPT = """\
{university_name}의 2026년 1학기(봄) 대학 축제 정보를 알고 있으면 JSON 배열로 반환해줘.
모르면 반드시 빈 배열 []만 반환해. 추측하지 말 것.

반환 형식 (JSON 배열만):
[{{"festival_name":"...","date_start":"YYYY-MM-DD","date_end":"YYYY-MM-DD","location":"...","lineup":[...],"confidence":0.0}}]
"""


def sb_get(table: str, params: dict) -> list:
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{table}", headers=SB_HEADERS, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def sb_upsert(table: str, row: dict, on_conflict: str = "id"):
    headers = {**SB_HEADERS, "Prefer": "resolution=merge-duplicates,return=minimal"}
    r = requests.post(f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}", headers=headers, json=row, timeout=15)
    r.raise_for_status()


def to_semester(date_str: str) -> str:
    from datetime import date
    d = date.fromisoformat(date_str[:10])
    return f"{d.year}-{1 if d.month <= 6 else 2}"


def make_hash(university_name: str, festival_name: str, date_start: str) -> str:
    raw = f"{university_name}|{festival_name}|{date_start}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _parse(raw: str) -> list[dict]:
    raw = re.sub(r"^```(?:json)?\n?", "", raw.strip())
    raw = re.sub(r"\n?```$", "", raw).strip()
    results = json.loads(raw)
    if not isinstance(results, list):
        return []
    valid = []
    for r in results:
        if not r.get("date_start") or not r.get("date_end"):
            continue
        if r.get("confidence", 0) < CONFIDENCE_THRESHOLD:
            continue
        try:
            if to_semester(r["date_start"]) != TARGET_SEMESTER:
                continue
        except Exception:
            continue
        valid.append(r)
    return valid


def search_with_gemini(university_name: str, keywords: str) -> list[dict]:
    prompt = SEARCH_PROMPT.format(university_name=university_name, keywords=keywords)
    try:
        resp = gemini.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())]
            ),
        )
        return _parse(resp.text)
    except Exception as e:
        err = str(e)
        if "429" in err or "RESOURCE_EXHAUSTED" in err:
            print("Gemini 한도 초과 → Groq 전환", flush=True)
            return None  # fallback 신호
        print(f"Gemini error: {e}")
        return []


def search_with_groq(university_name: str) -> list[dict]:
    prompt = GROQ_PROMPT.format(university_name=university_name)
    for model in ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]:
        try:
            resp = groq_client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
            )
            return _parse(resp.choices[0].message.content)
        except Exception as e:
            err = str(e)
            if "429" in err or "rate_limit" in err:
                print(f"Groq {model} 한도 초과 → 다음 모델", flush=True)
            else:
                print(f"Groq {model} error: {e}")
                return []
    print("모든 AI 한도 초과, skip")
    return []


def process(university: dict):
    name = university["name"]
    hint = university.get("festival_hint") or ""
    keywords = f"{name} 2026 축제 라인업 {hint}".strip()

    print(f"[{name}] ", end="", flush=True)

    festivals = search_with_gemini(name, keywords)
    if festivals is None:
        festivals = search_with_groq(name)

    if not festivals:
        print("정보 없음")
        return

    saved = []
    for festival in festivals:
        row = {
            "university_id": university["id"],
            "university_name": name,
            "festival_name": festival.get("festival_name"),
            "date_start": festival["date_start"],
            "date_end": festival["date_end"],
            "location": festival.get("location"),
            "lineup": festival.get("lineup") or [],
            "semester": to_semester(festival["date_start"]),
            "source_url": festival.get("source_url", ""),
            "source_name": "Gemini Search",
            "status": "draft",
            "confidence": festival.get("confidence"),
            "scraped_hash": make_hash(name, festival.get("festival_name", ""), festival["date_start"]),
        }
        try:
            sb_upsert("festivals", row, on_conflict="scraped_hash")
            saved.append(festival)
        except Exception as e:
            print(f"DB error: {e}")

    if saved:
        best = max(saved, key=lambda f: (f.get("confidence") or 0, len(f.get("lineup") or [])))
        date_range = f"{best['date_start']} ~ {best['date_end']}"
        lineup_str = ", ".join(best.get("lineup") or []) or "미확인"
        print(f"{date_range} | 라인업: {lineup_str}")

    time.sleep(4)  # Gemini 15 RPM 제한


def clear_drafts():
    r = requests.delete(
        f"{SUPABASE_URL}/rest/v1/festivals",
        headers=SB_HEADERS,
        params={"semester": f"eq.{TARGET_SEMESTER}", "status": "eq.draft"},
        timeout=15,
    )
    r.raise_for_status()
    print(f"기존 draft 삭제 완료 ({TARGET_SEMESTER})")


def main():
    print("=== Festival Collector Start ===")
    clear_drafts()
    unis = sb_get("universities", {"is_active": "eq.true", "select": "*"})
    print(f"Active universities: {len(unis)}\n")

    for uni in unis:
        try:
            process(uni)
        except Exception as e:
            print(f"[ERROR] {uni['name']}: {e}")

    print("\n=== Done ===")


if __name__ == "__main__":
    main()
