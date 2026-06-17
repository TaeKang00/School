# School Festival Collector

한국 대학 축제 정보를 자동으로 수집해 Supabase에 저장하는 GitHub Actions 기반 크롤러.

## 시스템 개요

```
GitHub Actions (매일 자정 KST)
  ├─ 네이버 뉴스 API  →  Gemini 분석  →  Supabase (status=draft)
  └─ RSSHub Instagram →  Gemini 분석  →  Supabase (status=draft)
```

- 수집된 데이터는 `draft` 상태로 저장됨
- 관리자가 Supabase 대시보드에서 검수 후 `published` / `rejected` 처리
- 중복 방지: `scraped_hash` (SHA-256) 기준 upsert

## 필요한 GitHub Secrets

저장소 → Settings → Secrets and variables → Actions → New repository secret

| Secret 이름            | 값                          |
|------------------------|-----------------------------|
| `SUPABASE_URL`         | Supabase 프로젝트 URL        |
| `SUPABASE_SERVICE_KEY` | service_role 키             |
| `GEMINI_API_KEY`       | Google AI Studio API 키     |
| `NAVER_CLIENT_ID`      | 네이버 개발자센터 Client ID  |
| `NAVER_CLIENT_SECRET`  | 네이버 개발자센터 Secret     |

## 운영 가이드

### draft 데이터 검수

Supabase 대시보드 → Table Editor → festivals

```sql
-- 검수 대기 목록 (confidence 높은 순)
SELECT university_name, festival_name, date_start, lineup, source_url, confidence
FROM festivals
WHERE status = 'draft'
ORDER BY confidence DESC, created_at DESC;

-- 승인
UPDATE festivals SET status = 'published' WHERE id = '<id>';

-- 거절
UPDATE festivals SET status = 'rejected' WHERE id = '<id>';
```

### 대학 추가

```sql
INSERT INTO universities (name, slug, region, instagram_handle)
VALUES ('한국대학교', 'hankook-univ', '서울', 'hankook_festival');
```

### 대학 수집 제외 (비활성화)

```sql
UPDATE universities SET is_active = false WHERE slug = 'hankook-univ';
```

### 인스타 핸들 채우기

```sql
UPDATE universities SET instagram_handle = 'yonsei_festival' WHERE slug = 'yonsei';
```

핸들은 Instagram URL `instagram.com/<handle>` 에서 확인.

### 수동 실행

GitHub → Actions → Festival Data Collector → Run workflow

## 트러블슈팅

### Gemini 할당량 초과 (429 오류)

`collect.py`의 `time.sleep(0.3)` 값을 늘리거나,
Google AI Studio에서 할당량 상향 신청.

무료 티어 한도: 1,500 req/day, 15 RPM.
대학 수 × 쿼리 수가 많을 경우 `display=5`로 줄이는 것도 방법.

### 특정 대학 계속 실패

로그에서 해당 대학 에러 메시지 확인:

```
[ERROR] 한국대학교: ...
```

원인별 대처:
- 네이버 API 오류 → 쿼리 문자열 특수문자 확인
- RSSHub 오류 → `instagram_handle` 오타 확인 또는 `NULL`로 초기화
- Gemini JSON 파싱 오류 → 해당 뉴스 내용이 너무 짧거나 무관한 경우 (자동 skip됨)

### 축제 날짜가 잘못 파싱되는 경우

Supabase에서 해당 row를 `rejected` 처리 후,
`source_url`을 직접 확인해 수동 입력.
