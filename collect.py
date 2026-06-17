import json
import hashlib
import re
import time
import requests
import feedparser
from google import genai
from groq import Groq

SUPABASE_URL = "https://kxtyoopunnwxjhvtxbca.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imt4dHlvb3B1bm53eGpodnR4YmNhIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3ODMyNjg3NSwiZXhwIjoyMDkzOTAyODc1fQ.-KK3F_62XnVI1Fl_VpSK5oI5irMznZu4sdaXkFPZ_f8"
GEMINI_API_KEY = "AIzaSyCfMmaJ7k58qtgwCUZiSo83EHI26VYGTCA"
GROQ_API_KEY = "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"
NAVER_CLIENT_ID = "kUOU2mpqPg2jbVhA9YfG"
NAVER_CLIENT_SECRET = "eG0JTp8U2L"

gemini = genai.Client(api_key=GEMINI_API_KEY)
groq_client = Groq(api_key=GROQ_API_KEY)

RSSHUB_BASE = "https://rsshub.app/instagram/user"
NAVER_SOURCES = {
    "뉴스": "https://openapi.naver.com/v1/search/news.json",
    "블로그": "https://openapi.naver.com/v1/search/blog.json",
    "웹문서": "https://openapi.naver.com/v1/search/webkr.json",
}
SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}

TARGET_SEMESTER = "2026-1"

# 대학 1개당 Gemini 1번 호출 — 수집한 모든 항목을 한 번에 분석
EXTRACT_PROMPT = """\
아래는 {university_name}의 2026년 축제 관련 수집 결과입니다. 번호가 매겨져 있습니다.

{content}

위 내용에서 2026년 1학기(1~6월) 축제 정보를 추출해 JSON 배열로만 반환하세요.
이미 지난 축제도 포함합니다. 정보가 없으면 빈 배열 []을 반환하세요.

반환 형식 (JSON 배열만, 마크다운 코드블록 없이):
[
  {{
    "festival_name": "축제명 또는 null",
    "date_start": "YYYY-MM-DD 또는 null",
    "date_end": "YYYY-MM-DD 또는 null",
    "location": "장소 또는 null",
    "lineup": ["아티스트1", "아티스트2"],
    "source_index": 0,
    "confidence": 0.0
  }}
]
source_index는 해당 정보를 찾은 항목의 번호(0부터 시작)입니다.
"""


def sb_get(table: str, params: dict) -> list:
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{table}", headers=SB_HEADERS, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def sb_upsert(table: str, row: dict, on_conflict: str = "id"):
    headers = {**SB_HEADERS, "Prefer": "resolution=merge-duplicates,return=minimal"}
    r = requests.post(f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}", headers=headers, json=row, timeout=15)
    r.raise_for_status()


def naver_search(url: str, query: str) -> list:
    headers = {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }
    try:
        r = requests.get(url, headers=headers, params={"query": query, "sort": "date", "display": 10}, timeout=10)
        if r.status_code == 200:
            return r.json().get("items", [])
        print(f"    Naver {r.status_code}: {r.text[:80]}")
    except Exception as e:
        print(f"    Naver error: {e}")
    return []


def instagram_posts(handle: str) -> list:
    try:
        feed = feedparser.parse(f"{RSSHUB_BASE}/{handle}", request_headers={"User-Agent": "Mozilla/5.0"})
        return feed.entries[:5]
    except Exception as e:
        print(f"    RSSHub error ({handle}): {e}")
    return []


def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


def to_semester(date_str: str) -> str:
    from datetime import date
    d = date.fromisoformat(date_str[:10])
    return f"{d.year}-{1 if d.month <= 6 else 2}"


def make_hash(source_url: str, university_name: str, content: str) -> str:
    raw = f"{source_url}|{university_name}|{strip_html(content)[:200]}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _parse_festivals(raw: str) -> list[dict]:
    raw = re.sub(r"^```(?:json)?\n?", "", raw.strip())
    raw = re.sub(r"\n?```$", "", raw).strip()
    results = json.loads(raw)
    if not isinstance(results, list):
        return []
    valid = []
    for r in results:
        if not r.get("date_start"):
            continue
        try:
            if to_semester(r["date_start"]) != TARGET_SEMESTER:
                continue
        except Exception:
            continue
        valid.append(r)
    return valid


def batch_analyze(university_name: str, items: list[dict]) -> list[dict]:
    if not items:
        return []

    numbered = "\n\n".join(
        f"[{i}] {strip_html(item['content'])[:300]}" for i, item in enumerate(items)
    )
    prompt = EXTRACT_PROMPT.format(university_name=university_name, content=numbered)

    # 1차: Gemini
    try:
        resp = gemini.models.generate_content(model="gemini-2.0-flash", contents=prompt)
        return _parse_festivals(resp.text)
    except Exception as e:
        err = str(e)
        if "429" in err or "RESOURCE_EXHAUSTED" in err:
            print(f"    Gemini 한도 초과 → Groq 전환")
        else:
            print(f"    Gemini error: {e}")
            return []  # Gemini 응답은 왔지만 파싱 실패 → Groq 불필요

    # 2차 fallback: Groq (llama-3.3-70b)
    try:
        resp = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
        )
        return _parse_festivals(resp.choices[0].message.content)
    except Exception as e:
        print(f"    Groq error: {e}")
        return []


def upsert(university: dict, items: list[dict], festivals: list[dict]):
    for festival in festivals:
        idx = festival.get("source_index", 0)
        item = items[idx] if 0 <= idx < len(items) else items[0]
        row = {
            "university_id": university["id"],
            "university_name": university["name"],
            "festival_name": festival.get("festival_name"),
            "date_start": festival["date_start"],
            "date_end": festival.get("date_end"),
            "location": festival.get("location"),
            "lineup": festival.get("lineup") or [],
            "semester": to_semester(festival["date_start"]),
            "source_url": item["url"],
            "source_name": item["source_name"],
            "status": "draft",
            "confidence": festival.get("confidence"),
            "scraped_hash": make_hash(item["url"], university["name"], item["content"]),
        }
        try:
            sb_upsert("festivals", row, on_conflict="scraped_hash")
            date_range = festival["date_start"]
            if festival.get("date_end"):
                date_range += f" ~ {festival['date_end']}"
            lineup_str = ", ".join(festival.get("lineup") or []) or "미확인"
            print(f"    {date_range} | 라인업: {lineup_str}")
        except Exception as e:
            print(f"    DB error: {e}")


def process(university: dict):
    name = university["name"]
    print(f"\n[{name}]", end=" ", flush=True)
    seen: set[str] = set()
    items: list[dict] = []

    # 네이버 검색 (3쿼리 × 3소스)
    for query in [f"{name} 축제", f"{name} 대동제", f"{name} 축제 라인업"]:
        for source_name, source_url in NAVER_SOURCES.items():
            for item in naver_search(source_url, query):
                url = item.get("link", "")
                if not url or url in seen:
                    continue
                seen.add(url)
                items.append({
                    "url": url,
                    "source_name": source_name,
                    "content": f"{item.get('title', '')}\n{item.get('description', '')}",
                })

    # Instagram RSS
    handle = university.get("instagram_handle")
    if handle:
        for post in instagram_posts(handle):
            url = post.get("link") or f"https://instagram.com/{handle}"
            if url in seen:
                continue
            seen.add(url)
            content = strip_html(post.get("summary", "") or post.get("title", ""))
            if content:
                items.append({"url": url, "source_name": "Instagram", "content": content})

    print(f"수집 {len(items)}건", flush=True)

    if not items:
        return

    festivals = batch_analyze(name, items[:30])  # 토큰 한도 대비 상위 30건만 분석
    if festivals:
        upsert(university, items, festivals)
    else:
        print(f"    축제 정보 없음")

    time.sleep(4)  # Gemini/Groq 15 RPM 제한 (60s/15 = 4s)



def main():
    print("=== Festival Collector Start ===")
    unis = sb_get("universities", {"is_active": "eq.true", "select": "*"})
    print(f"Active universities: {len(unis)}")

    for uni in unis:
        try:
            process(uni)
        except Exception as e:
            print(f"  [ERROR] {uni['name']}: {e}")

    print("\n=== Done ===")


if __name__ == "__main__":
    main()
