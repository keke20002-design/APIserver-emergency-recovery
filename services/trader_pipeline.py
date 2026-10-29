"""doc/trader.txt 전략 + 실시간 주식 스크리닝 → 종목 추천 블로그 포스팅 파이프라인."""
import hashlib
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path

import pytz
from google import genai
from google.genai import types

from services.blog_db import (
    init_db,
    mark_keyword_used,
    save_post_log,
    save_published_document,
)
from services.blog_pipeline import _inject_adsense, _inject_summary_box
from services.blog_wordpress_service import publish_post

logger = logging.getLogger(__name__)

KST = pytz.timezone("Asia/Seoul")
DOC_DIR = Path(__file__).parent.parent / "doc"
_MODEL = "gemini-2.5-flash"


def _make_client() -> genai.Client:
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not configured")
    return genai.Client(api_key=api_key)


def read_trader_doc() -> tuple[str, str]:
    path = DOC_DIR / "trader.txt"
    if not path.exists():
        raise FileNotFoundError(f"trader.txt not found: {path}")
    content = path.read_text(encoding="utf-8").strip()
    if not content:
        raise ValueError("trader.txt is empty")
    return content, hashlib.sha256(content.encode()).hexdigest()


def _parse_json_safe(text: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.MULTILINE)
    text = re.sub(r"\s*```$", "", text.strip(), flags=re.MULTILINE)
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # content 필드 안의 따옴표·개행으로 파싱 오류 시 주요 필드 개별 추출
        title_m   = re.search(r'"title"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
        meta_m    = re.search(r'"meta_description"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
        kw_m      = re.search(r'"keyword"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
        tags_m    = re.search(r'"tags"\s*:\s*(\[.*?\])', text, re.DOTALL)
        summary_m = re.search(r'"summary"\s*:\s*(\[.*?\])', text, re.DOTALL)
        tickers_m = re.search(r'"recommended_tickers"\s*:\s*(\[.*?\])', text, re.DOTALL)
        content_m = re.search(r'"content"\s*:\s*"(.*?)(?:"\s*[,}]|"\s*$)', text, re.DOTALL)
        if title_m and content_m:
            content_raw = content_m.group(1).replace('\\"', '"').replace("\\n", "\n")
            def _safe_list(m):
                try:
                    return json.loads(m.group(1)) if m else []
                except Exception:
                    return []
            return {
                "keyword":              kw_m.group(1) if kw_m else "",
                "title":                title_m.group(1),
                "meta_description":     meta_m.group(1) if meta_m else "",
                "tags":                 _safe_list(tags_m),
                "summary":              _safe_list(summary_m),
                "content":              content_raw,
                "recommended_tickers":  _safe_list(tickers_m),
            }
        raise


_META_PROMPT = """당신은 한국 주식시장 전문 투자 블로거입니다.

아래 투자 전략 원칙과 오늘({today}) 선별된 종목 데이터를 바탕으로
블로그 포스트 메타데이터를 JSON으로 출력하세요.

=== 투자 전략 원칙 ===
{strategy}
=== 전략 끝 ===

=== 오늘 추천 종목 데이터 ===
{screened_json}
=== 데이터 끝 ===

=== 금지어 ===
'태공', '미네르바' — 어디에도 사용 금지

SEO 키워드: {keyword}

JSON만 출력 (content 필드 없음):
{{
  "keyword": "{keyword}",
  "title": "20~35자 검색어형 제목 (날짜+종목추천 포함)",
  "meta_description": "100~160자",
  "tags": ["태그1", "태그2", "태그3", "태그4", "태그5"],
  "summary": ["핵심 포인트1 (15~30자)", "핵심 포인트2", "핵심 포인트3"],
  "recommended_tickers": ["종목코드1", "종목코드2"]
}}
"""

_META_FALLBACK_PROMPT = """당신은 한국 주식시장 전문 투자 블로거입니다.

오늘({today}) 투자 전략 포스트 메타데이터를 JSON으로 출력하세요.

SEO 키워드: {keyword}

JSON만 출력:
{{
  "keyword": "{keyword}",
  "title": "20~35자 검색어형 제목",
  "meta_description": "100~160자",
  "tags": ["태그1", "태그2", "태그3", "태그4", "태그5"],
  "summary": ["핵심 포인트1", "핵심 포인트2", "핵심 포인트3"],
  "recommended_tickers": []
}}
"""

_CONTENT_PROMPT = """당신은 한국 주식시장 전문 투자 블로거입니다.

아래 내용을 바탕으로 WordPress HTML 블로그 본문만 작성하세요.

=== 투자 전략 원칙 ===
{strategy}
=== 전략 끝 ===

=== 오늘 추천 종목 데이터 ({today}) ===
{screened_json}
=== 데이터 끝 ===

포스트 제목: {title}
SEO 키워드: {keyword}

=== 말투 원칙 (최우선) ===
- 친한 투자 고수가 자기 매매일지를 쓰듯 작성. 강의 말투 절대 금지.
- 권장 표현: 보니까 / 실제로 / 의외로 / 생각보다 / 이번에는 / 이 부분은
- 금지 표현: 안녕하세요 / 살펴보겠습니다 / 알아보겠습니다 / 소개해드립니다 / 여러분께

=== 작성 지침 ===
1. 종목 3~5개 선택, 각각 기술적 근거·패턴·매수포인트·손절가 제시
2. 구조: 종목 표 → 종목별 분석 → 매매 전략 → 면책조항
3. 면책조항 필수: "본 포스트는 투자 참고용이며, 투자 손익에 대한 책임은 투자자 본인에게 있습니다."
4. 제공된 데이터 외 임의 수치 창작 금지
5. 1500~3500자, h2/h3/table 필수 포함
6. 수익 보장 표현 절대 금지
7. '태공', '미네르바' 단어 절대 포함 금지

HTML 본문만 출력 (JSON 감싸기 없음, 마크다운 코드블록 없음):
"""

_CONTENT_FALLBACK_PROMPT = """당신은 한국 주식시장 전문 투자 블로거입니다.

오늘({today}) 기준, 투자 전략({keyword}) 주제로 WordPress HTML 블로그 본문을 작성하세요.

=== 투자 전략 원칙 ===
{strategy}
=== 전략 끝 ===

포스트 제목: {title}

말투: 친한 투자 고수가 자기 매매일지를 쓰듯. 강의 말투 금지.
구조: 오늘 시장 분위기 → 10일선 매매법 핵심 → 거래량 분석법 → 면책조항
면책조항 필수, 1500~3000자, h2/h3/table 포함.
'태공', '미네르바' 단어 절대 금지.

HTML 본문만 출력:
"""


async def _generate_post(
    doc_content: str,
    screened: list[dict],
    keyword: str,
    today_str: str,
) -> dict:
    client = _make_client()
    screened_json = json.dumps(screened, ensure_ascii=False, indent=2) if screened else "[]"

    # ── 1단계: 메타데이터 JSON (content 제외) ────────────────────────────────
    if screened:
        meta_prompt = _META_PROMPT.format(
            today=today_str,
            strategy=doc_content[:4000],
            screened_json=screened_json,
            keyword=keyword,
        )
    else:
        meta_prompt = _META_FALLBACK_PROMPT.format(today=today_str, keyword=keyword)

    meta: dict = {}
    last_err = None
    for attempt in range(1, 4):
        try:
            resp = await client.aio.models.generate_content(
                model=_MODEL,
                contents=meta_prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.4 + (attempt - 1) * 0.1,
                    max_output_tokens=1024,
                ),
            )
            meta = _parse_json_safe(resp.text)
            break
        except (json.JSONDecodeError, ValueError) as e:
            last_err = e
            logger.warning("Meta JSON parse failed (attempt %d/3): %s", attempt, e)
    if not meta:
        raise RuntimeError(f"Gemini 메타데이터 JSON 파싱 3회 모두 실패: {last_err}")

    # ── 2단계: HTML 본문 (JSON 없이 순수 HTML) ───────────────────────────────
    title = meta.get("title", keyword)
    if screened:
        content_prompt = _CONTENT_PROMPT.format(
            today=today_str,
            strategy=doc_content[:4000],
            screened_json=screened_json,
            title=title,
            keyword=keyword,
        )
    else:
        content_prompt = _CONTENT_FALLBACK_PROMPT.format(
            today=today_str,
            strategy=doc_content[:4000],
            title=title,
            keyword=keyword,
        )

    html_content = ""
    for attempt in range(1, 4):
        try:
            resp = await client.aio.models.generate_content(
                model=_MODEL,
                contents=content_prompt,
                config=types.GenerateContentConfig(
                    temperature=0.5 + (attempt - 1) * 0.1,
                    max_output_tokens=8192,
                ),
            )
            raw = resp.text.strip()
            raw = re.sub(r"^```(?:html)?\s*", "", raw, flags=re.MULTILINE)
            raw = re.sub(r"\s*```$", "", raw.strip(), flags=re.MULTILINE)
            raw = raw.strip()
            # 금지어 포함 h2/h3 섹션 제거, 단어 자체도 삭제
            for word in ("태공", "미네르바"):
                raw = re.sub(rf"<h[23][^>]*>[^<]*{word}[^<]*</h[23]>.*?(?=<h[23]|\Z)", "", raw, flags=re.DOTALL)
                raw = raw.replace(word, "")
            html_content = raw.strip()
            if html_content:
                break
        except Exception as e:
            logger.warning("Content generation failed (attempt %d/3): %s", attempt, e)
    if not html_content:
        raise RuntimeError("Gemini HTML 본문 생성 3회 모두 실패")

    meta["content"] = html_content
    return meta


async def run_trader_pipeline(wp_status: str = "publish") -> dict:
    """
    trader.txt 전략 + 실시간 KOSPI/KOSDAQ 스크리닝 → 종목 추천 포스트 → WordPress 발행.
    매일 고유 날짜 키워드로 발행 (하루 1회 자동 실행).
    """
    init_db()

    # 1. 전략 문서 로드
    doc_content, doc_hash = read_trader_doc()
    logger.info("Trader doc — hash=%s len=%d", doc_hash[:12], len(doc_content))

    # 2. 실시간 주식 스크리닝
    screened: list[dict] = []
    try:
        from services.stock_screener import screen_kospi_kosdaq
        screened = await screen_kospi_kosdaq(max_workers=8, top_n=8)
        logger.info("Screening — %d candidates", len(screened))
    except Exception as e:
        logger.error("Screening failed: %s — fallback to strategy-only post", e)

    # 3. 날짜 + 요일 기반 키워드 (중복 감점 방지)
    now = datetime.now(KST)
    today_str = now.strftime("%Y년 %m월 %d일")
    _weekday_angle = {0: "이번 주 주도주", 2: "수요일 반등 종목", 4: "주말 보유 우량주"}
    angle = _weekday_angle.get(now.weekday(), "주식 추천 종목")
    keyword = now.strftime("%m월%d일") + f" {angle}"

    # 4. 포스트 생성
    post_data = await _generate_post(doc_content, screened, keyword, today_str)

    title = post_data["title"]
    summary = post_data.get("summary", [])
    tags = post_data.get("tags", [])
    meta_description = post_data.get("meta_description", "")
    recommended = post_data.get("recommended_tickers", [])

    content = _inject_summary_box(post_data["content"], summary)
    content = _inject_adsense(content)

    # 5. WordPress 발행
    wp_result = await publish_post(
        title=title,
        content=content,
        tags=tags,
        category="Market",
        meta_description=meta_description,
        status=wp_status,
    )
    wp_post_id = wp_result["post_id"]
    wp_url = wp_result["url"]

    # 6. 로그 저장
    save_published_document(doc_hash, keyword, wp_post_id, wp_url)
    mark_keyword_used(keyword, "Market")
    save_post_log(
        keyword=keyword,
        topic="Market",
        title=title,
        wp_post_id=wp_post_id,
        wp_url=wp_url,
        status=wp_result["status"],
    )

    result = {
        "keyword": keyword,
        "screened_count": len(screened),
        "recommended_tickers": recommended,
        "title": title,
        "tags": tags,
        "wp_post_id": wp_post_id,
        "wp_url": wp_url,
        "wp_status": wp_result["status"],
        "doc_hash": doc_hash[:12],
    }
    logger.info(
        "Trader pipeline done — post_id=%s title='%s' tickers=%s",
        wp_post_id, title, recommended,
    )

    # 검색엔진 핑
    try:
        from services.blog_ping_service import ping_search_engines
        await ping_search_engines(wp_url)
    except Exception as e:
        logger.warning("Trader post search engine ping failed: %s", e)

    return result
