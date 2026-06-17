import json
import hashlib
import re
import time
import requests
import feedparser
from google import genai

SUPABASE_URL = "https://kxtyoopunnwxjhvtxbca.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imt4dHlvb3B1bm53eGpodnR4YmNhIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3ODMyNjg3NSwiZXhwIjoyMDkzOTAyODc1fQ.-KK3F_62XnVI1Fl_VpSK5oI5irMznZu4sdaXkFPZ_f8"
GEMINI_API_KEY = "AIzaSyCfMmaJ7k58qtgwCUZiSo83EHI26VYGTCA"
NAVER_CLIENT_ID = "ZMCGvUd9yOl8MTAOpE7n"
NAVER_CLIENT_SECRET = "XskOZ3kD5L"

gemini = genai.Client(api_key=GEMINI_API_KEY)

RSSHUB_BASE = "https://rsshub.app/instagram/user"
NAVER_NEWS_URL = "https://openapi.naver.com/v1/search/news.json"
SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}

TARGET_SEMESTER = "2026-1"  # 수집 대상 학기

EXTRACT_PROMPT = """\
한국 대학교 축제 뉴스/게시물입니다.
대학교: {university_name}

내용:
{content}

위 내용에서 2026년 1학기(1~6월) 축제 정보를 추출해 JSON으로만 반환하세요.
이미 지난 축제도 포함합니다. 2026년 1학기 축제가 아니거나 날짜를 알 수 없으면 null을 반환하세요.

반환 형식 (JSON만, 마크다운 코드블록 없이):
{{
  "festival_name": "축제명 또는 null",
  "date_start": "YYYY-MM-DD 또는 null",
  "date_end": "YYYY-MM-DD 또는 null",
  "location": "장소 또는 null",
  "lineup": ["아티스트1", "아티스트2"],
  "confidence": 0.0~1.0
}}
"""


def sb_get(table: str, params: dict) -> list:
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{table}", headers=SB_HEADERS, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def sb_upsert(table: str, row: dict, on_conflict: str = "id"):
    headers = {**SB_HEADERS, "Prefer": "resolution=merge-duplicates,return=minimal"}
    r = requests.post(f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}", headers=headers, json=row, timeout=15)
    r.raise_for_status()


def naver_news(query: str) -> list:
    headers = {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }
    try:
        r = requests.get(NAVER_NEWS_URL, headers=headers, params={"query": query, "sort": "date", "display": 10}, timeout=10)
        if r.status_code == 200:
            return r.json().get("items", [])
    except Exception as e:
        print(f"    Naver error ({query}): {e}")
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


def analyze(university_name: str, content: str, debug: bool = False) -> dict | None:
    content = strip_html(content)[:3000]
    prompt = EXTRACT_PROMPT.format(university_name=university_name, content=content)
    try:
        resp = gemini.models.generate_content(model="gemini-2.0-flash", contents=prompt)
        raw = resp.text.strip()
        if debug:
            print(f"  [debug] Gemini 응답: {raw[:100]}")
        raw = re.sub(r"^```(?:json)?\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw).strip()
        if raw == "null":
            return None
        data = json.loads(raw)
        if not data.get("date_start"):
            return None
        if to_semester(data["date_start"]) != TARGET_SEMESTER:
            return None
        return data
    except Exception as e:
        print(f"    Gemini error: {e}")
        return None


def make_hash(source_url: str, university_name: str, content: str) -> str:
    raw = f"{source_url}|{university_name}|{strip_html(content)[:200]}"
    return hashlib.sha256(raw.encode()).hexdigest()


def to_semester(date_str: str) -> str:
    from datetime import date
    d = date.fromisoformat(date_str[:10])
    return f"{d.year}-{1 if d.month <= 6 else 2}"


def upsert(university: dict, source_url: str, source_name: str, content: str, analysis: dict):
    row = {
        "university_id": university["id"],
        "university_name": university["name"],
        "festival_name": analysis.get("festival_name"),
        "date_start": analysis["date_start"],
        "date_end": analysis.get("date_end"),
        "location": analysis.get("location"),
        "lineup": analysis.get("lineup") or [],
        "semester": to_semester(analysis["date_start"]),
        "source_url": source_url,
        "source_name": source_name,
        "status": "draft",
        "confidence": analysis.get("confidence"),
        "scraped_hash": make_hash(source_url, university["name"], content),
    }
    try:
        sb_upsert("festivals", row, on_conflict="scraped_hash")
        print(f"    saved: {analysis.get('festival_name') or '(이름미상)'} ({analysis['date_start']}, {row['semester']})")
    except Exception as e:
        print(f"    DB error: {e}")


def process(university: dict, debug: bool = False):
    name = university["name"]
    print(f"\n[{name}]")
    seen: set[str] = set()

    year = TARGET_SEMESTER.split("-")[0]
    for query in [f"{name} {year} 축제", f"{name} {year} 대동제", f"{name} {year} 축제 라인업"]:
        items = naver_news(query)
        if debug:
            print(f"  [debug] '{query}' → {len(items)}건")
        for item in items:
            url = item.get("link", "")
            if not url or url in seen:
                continue
            seen.add(url)
            content = f"{item.get('title', '')}\n{item.get('description', '')}"
            if debug:
                print(f"  [debug] 분석: {strip_html(item.get('title', ''))[:50]}")
            result = analyze(name, content, debug=debug)
            if result:
                upsert(university, url, "네이버뉴스", content, result)
            time.sleep(0.3)

    handle = university.get("instagram_handle")
    if handle:
        for post in instagram_posts(handle):
            url = post.get("link") or f"https://instagram.com/{handle}"
            if url in seen:
                continue
            seen.add(url)
            content = strip_html(post.get("summary", "") or post.get("title", ""))
            if not content:
                continue
            result = analyze(name, content)
            if result:
                upsert(university, url, "Instagram", content, result)
            time.sleep(0.3)


def main():
    print("=== Festival Collector Start ===")
    unis = sb_get("universities", {"is_active": "eq.true", "select": "*"})
    print(f"Active universities: {len(unis)}")

    for i, uni in enumerate(unis):
        try:
            process(uni, debug=(i < 3))
        except Exception as e:
            print(f"  [ERROR] {uni['name']}: {e}")

    print("\n=== Done ===")


if __name__ == "__main__":
    main()
