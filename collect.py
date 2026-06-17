import os
import json
import hashlib
import re
import time
import requests
import feedparser
from supabase import create_client, Client
import google.generativeai as genai

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
NAVER_CLIENT_ID = os.environ["NAVER_CLIENT_ID"]
NAVER_CLIENT_SECRET = os.environ["NAVER_CLIENT_SECRET"]

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
genai.configure(api_key=GEMINI_API_KEY)
gemini = genai.GenerativeModel("gemini-2.0-flash")

RSSHUB_BASE = "https://rsshub.app/instagram/user"
NAVER_NEWS_URL = "https://openapi.naver.com/v1/search/news.json"

EXTRACT_PROMPT = """\
한국 대학교 축제 뉴스/게시물입니다.
대학교: {university_name}

내용:
{content}

위 내용에서 축제 정보를 추출해 JSON으로만 반환하세요.
축제 관련 내용이 없으면 null을 반환하세요.

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


def naver_news(query: str) -> list[dict]:
    headers = {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }
    params = {"query": query, "sort": "date", "display": 10}
    try:
        r = requests.get(NAVER_NEWS_URL, headers=headers, params=params, timeout=10)
        if r.status_code == 200:
            return r.json().get("items", [])
    except Exception as e:
        print(f"    Naver API error ({query}): {e}")
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


def analyze(university_name: str, content: str) -> dict | None:
    content = strip_html(content)[:3000]
    prompt = EXTRACT_PROMPT.format(university_name=university_name, content=content)
    try:
        resp = gemini.generate_content(prompt)
        raw = resp.text.strip()
        # Gemini가 코드블록으로 감싸는 경우 처리
        raw = re.sub(r"^```(?:json)?\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        raw = raw.strip()
        if raw == "null":
            return None
        data = json.loads(raw)
        # date_start 없으면 의미없는 데이터
        if not data.get("date_start"):
            return None
        return data
    except Exception as e:
        print(f"    Gemini error: {e}")
        return None


def make_hash(source_url: str, university_name: str, content: str) -> str:
    raw = f"{source_url}|{university_name}|{strip_html(content)[:200]}"
    return hashlib.sha256(raw.encode()).hexdigest()


def to_semester(date_str: str) -> str:
    """'YYYY-MM-DD' → '2026-1' or '2026-2'"""
    year, month = int(date_str[:4]), int(date_str[5:7])
    half = 1 if month <= 6 else 2
    return f"{year}-{half}"


def upsert(university: dict, source_url: str, source_name: str, content: str, analysis: dict):
    scraped_hash = make_hash(source_url, university["name"], content)
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
        "scraped_hash": scraped_hash,
    }
    try:
        supabase.table("festivals").upsert(row, on_conflict="scraped_hash").execute()
        print(f"    saved: {analysis.get('festival_name') or '(이름미상)'} ({analysis['date_start']}, {row['semester']})")
    except Exception as e:
        print(f"    DB upsert error: {e}")


def process(university: dict):
    name = university["name"]
    print(f"\n[{name}]")
    seen: set[str] = set()

    # 네이버 뉴스 3가지 쿼리
    for query in [f"{name} 축제", f"{name} 축제 라인업", f"{name} 대동제"]:
        for item in naver_news(query):
            url = item.get("link", "")
            if not url or url in seen:
                continue
            seen.add(url)
            content = f"{item.get('title', '')}\n{item.get('description', '')}"
            result = analyze(name, content)
            if result:
                upsert(university, url, "네이버뉴스", content, result)
            time.sleep(0.3)  # Gemini RPM 여유

    # Instagram RSS (핸들이 있을 때만)
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
    unis = supabase.table("universities").select("*").eq("is_active", True).execute().data
    print(f"Active universities: {len(unis)}")

    for uni in unis:
        try:
            process(uni)
        except Exception as e:
            print(f"  [ERROR] {uni['name']}: {e}")

    print("\n=== Done ===")


if __name__ == "__main__":
    main()
