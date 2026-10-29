"""블로그 자동 포스팅 파이프라인 — 뉴스 수집 → 해설형 콘텐츠 생성 → WordPress 발행."""
import asyncio
import logging
import os
from datetime import datetime

import pytz

from services.blog_db import init_db, mark_keyword_used, save_post_log, get_recent_posts
from services.blog_naver_service import collect_hot_news, pick_fallback_keyword
from services.blog_gemini_service import generate_analytical_post, generate_blog_post, generate_english_post, generate_english_guide_post, generate_kr_guide_post, generate_post_image
from services.blog_wordpress_service import publish_post, upload_image, edit_post

logger = logging.getLogger(__name__)

KST = pytz.timezone("Asia/Seoul")

# 하루 최대 발행 수 (한국어+영어 슬롯당 2개 × 4슬롯 = 8, 여유 2)
MAX_POSTS_PER_DAY = 3

# 시간대별 카테고리 우선순위
SLOT_CATEGORY_HINT: dict[str, str] = {
    "morning": "AI",
    "afternoon": "AI",
    "evening": "AI",
}

_MAX_RETRIES = 3
_RETRY_DELAY = 5

# ── 네이버 점수 계산 ──────────────────────────────────────────────────────────

_CTR_POWER_WORDS = ['왜', '이유', '정리', '직접', '총정리', 'TOP', '비교', '충격', '진실', '논란', '결국', '뭐야', '핵심']
_DOPAMINE_EMOTIONAL = ['난리', '충격', '논란', '사실', '진실', '결국', '뭐야', '대박', '실화', '어떻게', '갑자기', '드디어']
_DOPAMINE_URGENCY   = ['지금', '최근', '요즘', '갑자기', '드디어', '마침내', '이제', '벌써']

_CATEGORY_BONUS = {
    '연예': 5, '스포츠': 4, '이슈': 4, '사회': 4,
    '경제': 2, '주식': 2, '금융': 2,
    'AI': 1, 'IT': 1,
    'Guide': -8, 'Guides': -8, 'Automation': -5,
}


def _calc_scores(title: str, content: str, category: str, gemini_score: int | None) -> dict:
    """Traffic Score 포함 전체 점수 계산."""
    import re

    # ── CTR Score ────────────────────────────────────────────────────────────
    ctr = 50
    found_power = sum(1 for w in _CTR_POWER_WORDS if w in title)
    ctr += min(found_power * 5, 15)
    if re.search(r'\d', title):      ctr += 10
    if title.endswith('?'):          ctr += 10
    title_len = len(title)
    if title_len <= 30:              ctr += 10
    elif title_len >= 35:            ctr -= 10
    ctr = max(0, min(100, ctr))

    # ── SEO Score ────────────────────────────────────────────────────────────
    seo = 40
    if 15 <= title_len <= 35:        seo += 15
    elif 10 <= title_len <= 40:      seo += 5
    h2_count = len(re.findall(r'<h2', content, re.IGNORECASE))
    seo += min(h2_count * 7, 21)
    content_len = len(re.sub(r'<[^>]+>', '', content))
    if content_len >= 1500:          seo += 15
    elif content_len >= 1000:        seo += 8
    if 'meta_description' in content or len(content) > 100: seo += 5
    seo = max(0, min(100, seo))

    # ── AI 감지 위험도 ────────────────────────────────────────────────────────
    plain = re.sub(r'<[^>]+>', '', content)
    sentences = [s.strip() for s in re.split(r'[.。!?]', plain) if len(s.strip()) > 10]
    endings = re.findall(r'입니다|습니다|됩니다', plain)
    ending_ratio = len(endings) / max(len(sentences), 1)
    avg_len = sum(len(s) for s in sentences) / max(len(sentences), 1)
    ai_risk_score = 0
    if ending_ratio > 0.5:   ai_risk_score += 2
    if ending_ratio > 0.7:   ai_risk_score += 1
    if avg_len > 60:         ai_risk_score += 1
    if avg_len > 80:         ai_risk_score += 1
    ai_risk = "HIGH" if ai_risk_score >= 3 else ("MEDIUM" if ai_risk_score >= 1 else "LOW")

    # ── 도파민 점수 ───────────────────────────────────────────────────────────
    dopa = 40
    dopa += min(sum(1 for w in _DOPAMINE_EMOTIONAL if w in title) * 8, 24)
    dopa += min(sum(1 for w in _DOPAMINE_URGENCY   if w in title) * 5, 15)
    dopa += min(found_power * 5, 10)
    if re.search(r'\d', title): dopa += 5
    dopa = max(0, min(100, dopa))

    # ── Traffic Score (Gemini 70% + Heuristic 30%) ───────────────────────────
    cat_bonus = _CATEGORY_BONUS.get(category, 0)
    heuristic = (gemini_score or 50) + cat_bonus
    ctr_bonus = min(found_power * 2, 8)
    heuristic += ctr_bonus
    if title_len >= 35: heuristic -= 5
    heuristic = max(0, min(100, heuristic))

    if gemini_score is not None:
        traffic = int(gemini_score * 0.7 + heuristic * 0.3)
    else:
        traffic = heuristic
    traffic = max(0, min(100, traffic))

    return {
        "traffic_score": traffic,
        "ctr_score": ctr,
        "seo_score": seo,
        "ai_risk": ai_risk,
        "dopamine_score": dopa,
    }


_BLOG_CSS = """<style>
@import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.css');
.entry-content,.post-content{font-family:'Pretendard',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:760px;margin-left:auto;margin-right:auto;}
.entry-content p,.post-content p,p{line-height:1.9;margin-bottom:28px;}
.entry-content h2,.post-content h2,h2{font-size:34px;line-height:1.4;margin-top:60px;margin-bottom:24px;font-weight:800;border-left:5px solid #2563eb;padding-left:15px;}
.entry-content strong,.post-content strong,strong{color:#2563eb;font-style:normal;}
.summary-box{background:#f8fafc;border-left:5px solid #2563eb;padding:20px;border-radius:12px;margin:25px 0;}
.summary-box h3{margin:0 0 10px;font-size:15px;color:#1e40af;}
.summary-box ul{margin:0;padding-left:18px;}
.summary-box li{padding:3px 0;color:#1f2937;font-size:15px;line-height:1.6;}
.quote-box{padding:20px;background:#f8fafc;border-left:4px solid #2563eb;margin:30px 0;border-radius:10px;font-size:16px;line-height:1.8;color:#1f2937;}
.point-box{background:#eff6ff;padding:18px;border-radius:10px;font-weight:600;margin:20px 0;color:#1e3a8a;font-size:15px;line-height:1.7;}
.stat-grid{display:flex;gap:15px;margin:25px 0;flex-wrap:wrap;}
.stat-item{flex:1;min-width:90px;padding:20px;background:#f8fafc;border-radius:12px;text-align:center;}
.stat-item h3{margin:0 0 4px;font-size:22px;color:#2563eb;font-weight:800;}
.stat-item p{margin:0;font-size:13px;color:#6b7280;}
.my-opinion{background:#fefce8;border-left:4px solid #f59e0b;padding:18px 20px;border-radius:0 10px 10px 0;margin:24px 0;}
.my-opinion h3{margin:0 0 8px;font-size:14px;color:#92400e;}
.my-opinion p{margin:0;color:#1f2937;font-size:15px;line-height:1.7;}
.checklist{background:#f0fdf4;border:1px solid #bbf7d0;padding:18px 20px;border-radius:12px;margin:24px 0;}
.checklist h3{margin:0 0 10px;font-size:15px;color:#15803d;}
.checklist ul{margin:0;padding-left:18px;}
.checklist li{padding:4px 0;color:#1f2937;font-size:14px;}
.conclusion-box{background:#1e3a8a;color:white;padding:24px;border-radius:14px;margin:28px 0;}
.conclusion-box h3{margin:0 0 10px;font-size:16px;color:#bfdbfe;}
.conclusion-box p{margin:0;font-size:15px;line-height:1.8;}
.faq-item{margin:16px 0;}
.faq-q{font-size:16px;font-weight:700;color:#1e3a8a;margin-bottom:6px;}
.faq-a{color:#374151;line-height:1.8;}
mark{background:none !important;color:#1d4ed8 !important;font-weight:700 !important;border-bottom:2px solid #93c5fd !important;padding:0 !important;border-radius:0 !important;}
.conclusion-box mark{background:none !important;color:#fef08a !important;border-bottom:none !important;}
.conclusion-box h2,.conclusion-box h3{color:#bfdbfe !important;border:none !important;border-left:none !important;border-bottom:none !important;padding-left:0 !important;margin-top:0 !important;}
.entry-content a,.post-content a,a{color:#2563eb;text-decoration:none;border-bottom:1px solid #93c5fd;}
.conclusion-box a,.conclusion-box p a{color:#bfdbfe !important;border-bottom:1px solid rgba(191,219,254,0.4) !important;}
</style>
<script>
(function(){var K='keke_pg';var w=document.getElementById('keke-cat-posts');var pg=new URLSearchParams(location.search).get('cat_page');var sv=sessionStorage.getItem(K);if(sv&&!pg){sessionStorage.removeItem(K);location.replace(location.pathname+'?cat_page='+sv);return;}if(!w)return;if(pg){sessionStorage.setItem(K,pg);w.querySelectorAll('a[href]').forEach(function(a){if(a.href.indexOf('cat_page')!==-1)return;a.addEventListener('click',function(){sessionStorage.setItem(K,pg);});try{var u=new URL(a.href);if(u.host===location.host){u.searchParams.set('cat_page',pg);a.href=u.toString();}}catch(e){}});}})();
</script>\n"""


def _inject_summary_box(content: str, summary: list[str], lang: str = "ko") -> str:
    """본문 최상단에 CSS + 요약 박스 삽입."""
    if not summary:
        return _BLOG_CSS + content
    label = "📌 Key Takeaways" if lang == "en" else "📌 3줄 요약"
    items_html = "".join(f"<li>{item}</li>" for item in summary[:3])
    box = (
        '<div class="summary-box">'
        f"<h3>{label}</h3>"
        f"<ul>{items_html}</ul>"
        "</div>\n"
    )
    return _BLOG_CSS + box + content


def _inject_adsense(content: str) -> str:
    """두 번째 </h2> 뒤에 인아티클 광고 삽입. 슬롯 ID 없으면 Auto Ads에 위임."""
    pub_id = os.getenv("ADSENSE_PUB_ID", "ca-pub-6848418595819302")
    slot_id = os.getenv("ADSENSE_AD_SLOT", "")
    if not slot_id:
        return content  # Auto Ads가 자동 처리

    ad_html = (
        '<div style="text-align:center;margin:32px 0;">'
        '<ins class="adsbygoogle"'
        ' style="display:block;text-align:center;"'
        ' data-ad-layout="in-article"'
        ' data-ad-format="fluid"'
        f' data-ad-client="{pub_id}"'
        f' data-ad-slot="{slot_id}"></ins>'
        '<script>(adsbygoogle = window.adsbygoogle || []).push({});</script>'
        '</div>'
    )

    # 두 번째 </h2> 뒤에 삽입
    idx = content.find("</h2>")
    if idx != -1:
        idx2 = content.find("</h2>", idx + 1)
        insert_pos = (idx2 + 5) if idx2 != -1 else (idx + 5)
        return content[:insert_pos] + "\n" + ad_html + "\n" + content[insert_pos:]
    return content + "\n" + ad_html


_ADSTERRA_NATIVE_BANNER = (
    '<div style="text-align:center;margin:32px 0;">'
    '<script async="async" data-cfasync="false" '
    'src="https://pl30445950.effectivecpmnetwork.com/8528a1d56e13f9e40f37f2654987be95/invoke.js"></script>'
    '<div id="container-8528a1d56e13f9e40f37f2654987be95"></div>'
    '</div>'
)


def _inject_adsterra_native(content: str) -> str:
    """Adsterra Native Banner 삽입 — 첫 번째 </h2> 뒤 (AdSense 인아티클 광고보다 위쪽)."""
    idx = content.find("</h2>")
    insert_pos = (idx + 5) if idx != -1 else len(content)
    return content[:insert_pos] + "\n" + _ADSTERRA_NATIVE_BANNER + "\n" + content[insert_pos:]


_ADSTERRA_BANNER_728x90 = (
    '<style>.keke-ad728{text-align:center;margin:24px 0;}'
    '@media (max-width:767px){.keke-ad728{display:none;}}</style>'
    '<div class="keke-ad728">'
    "<script>atOptions = {"
    "'key' : 'c97213489498569294a25386034fb4a0',"
    "'format' : 'iframe',"
    "'height' : 90,"
    "'width' : 728,"
    "'params' : {}"
    "};</script>"
    '<script src="https://www.highperformanceformat.com/c97213489498569294a25386034fb4a0/invoke.js"></script>'
    '</div>'
)


def _inject_adsterra_banner(content: str) -> str:
    """Adsterra 728x90 배너 삽입 — PC(768px 이상)에서만 노출, 첫 <h2> 바로 앞."""
    idx = content.find("<h2")
    insert_pos = idx if idx != -1 else len(content)
    return content[:insert_pos] + _ADSTERRA_BANNER_728x90 + "\n" + content[insert_pos:]


_ADSTERRA_BANNER_300x250 = (
    '<style>.keke-ad300{text-align:center;margin:24px 0;}'
    '@media (min-width:768px){.keke-ad300{display:none;}}</style>'
    '<div class="keke-ad300">'
    "<script>atOptions = {"
    "'key' : 'eb1f8c737cbec892458beb46a084d2e1',"
    "'format' : 'iframe',"
    "'height' : 250,"
    "'width' : 300,"
    "'params' : {}"
    "};</script>"
    '<script src="https://www.highperformanceformat.com/eb1f8c737cbec892458beb46a084d2e1/invoke.js"></script>'
    '</div>'
)


def _inject_adsterra_banner_mobile(content: str) -> str:
    """Adsterra 300x250 배너 삽입 — 모바일(768px 미만)에서만 노출, 첫 <h2> 바로 앞."""
    idx = content.find("<h2")
    insert_pos = idx if idx != -1 else len(content)
    return content[:insert_pos] + _ADSTERRA_BANNER_300x250 + "\n" + content[insert_pos:]


_ADSTERRA_SOCIAL_BAR = (
    '<script src="https://pl30567401.effectivecpmnetwork.com/fa/a1/96/'
    'faa196421c9e8ce81894a17e95879634.js"></script>'
)


def _inject_adsterra_social_bar(content: str) -> str:
    """Adsterra Social Bar 삽입 — 특정 위치 무관(자체적으로 화면에 오버레이 마운트), 본문 끝에 1회."""
    return content + "\n" + _ADSTERRA_SOCIAL_BAR


def _inject_internal_links(content: str, related_posts: list[dict], lang: str = "ko") -> str:
    """본문 끝에 관련 포스트 링크 박스 삽입."""
    if not related_posts:
        return content
    label = "📖 Related Posts" if lang == "en" else "📖 관련 포스트"
    items = "".join(
        f'<li><a href="{p["wp_url"]}">{p["title"]}</a></li>'
        for p in related_posts
    )
    box = (
        '<div class="summary-box" style="margin-top:32px;">'
        f'<h3>{label}</h3>'
        f'<ul>{items}</ul>'
        '</div>\n'
    )
    return content + "\n" + box


def _inject_json_ld(
    title: str,
    description: str,
    wp_url: str,
    lang: str = "ko",
    image_url: str = "",
    tags: list[str] | None = None,
) -> str:
    """Article JSON-LD Schema를 포스트 앞에 삽입 (Google 리치 스니펫용)."""
    import json as _json
    from datetime import datetime as _dt
    now_iso = _dt.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    schema = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": (title or "")[:110],
        "description": (description or "")[:160],
        "url": wp_url,
        "datePublished": now_iso,
        "dateModified": now_iso,
        "author": {
            "@type": "Person",
            "name": "KekeGroup",
            "url": "https://blog.kekegroup.uk",
        },
        "publisher": {
            "@type": "Organization",
            "name": "KekeGroup Blog",
            "logo": {
                "@type": "ImageObject",
                "url": "https://blog.kekegroup.uk/wp-content/uploads/kekegroup-logo.png",
            },
        },
        "inLanguage": "ko-KR" if lang == "ko" else "en-US",
        "mainEntityOfPage": {"@type": "WebPage", "@id": wp_url},
    }
    if image_url:
        schema["image"] = {
            "@type": "ImageObject",
            "url": image_url,
            "width": 1200,
            "height": 630,
        }
    if tags:
        schema["keywords"] = ", ".join(tags[:8])
    return (
        '<script type="application/ld+json">\n'
        + _json.dumps(schema, ensure_ascii=False, indent=2)
        + '\n</script>\n'
    )


def get_current_slot() -> str:
    hour = datetime.now(KST).hour
    if 5 <= hour < 11:
        return "morning"
    elif 11 <= hour < 17:
        return "afternoon"
    else:
        return "evening"


def _count_today_posts() -> int:
    today = datetime.now(KST).strftime("%Y-%m-%d")
    posts = get_recent_posts(limit=20)
    return sum(
        1 for p in posts
        if p.get("created_at", "").startswith(today) and p.get("status") != "FAILED"
    )


async def _run_with_retry(coro_fn, retries: int = _MAX_RETRIES, delay: int = _RETRY_DELAY):
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            return await coro_fn()
        except Exception as e:
            last_err = e
            logger.warning("Attempt %d/%d failed: %s", attempt, retries, e)
            if attempt < retries:
                await asyncio.sleep(delay * attempt)
    raise last_err


async def run_blog_pipeline(
    slot: str | None = None,
    wp_status: str = "draft",
    force: bool = False,
) -> dict:
    """
    전체 파이프라인 실행.
    - slot: morning | afternoon | evening (None이면 현재 시간 기준 자동)
    - wp_status: draft | publish | private
    - force: 하루 최대 발행 수 무시
    """
    init_db()

    chosen_slot = slot or get_current_slot()

    # 하루 최대 발행 수 체크
    if not force:
        today_count = _count_today_posts()
        if today_count >= MAX_POSTS_PER_DAY:
            raise RuntimeError(
                f"오늘 이미 {today_count}개 발행됨. 하루 최대 {MAX_POSTS_PER_DAY}개 제한."
            )

    logger.info("=== Blog pipeline start — slot=%s ===", chosen_slot)

    # 1. 핫 뉴스 수집
    hot_issue = await _run_with_retry(lambda: collect_hot_news(chosen_slot))

    if hot_issue and hot_issue.get("news"):
        news_items = hot_issue["news"]
        category = hot_issue.get("category", SLOT_CATEGORY_HINT.get(chosen_slot, "AI"))
        logger.info("News collected — query='%s', items=%d", hot_issue["query"], len(news_items))

        # 2. 한국어 콘텐츠 생성
        _ni, _sl = news_items, chosen_slot
        post_data = await _run_with_retry(lambda: generate_analytical_post(_ni, _sl))
        keyword = post_data["keyword"]

    else:
        # 폴백: 뉴스 없으면 키워드 기반 생성
        logger.warning("No hot news found, falling back to keyword-based generation")
        keyword = await pick_fallback_keyword(chosen_slot)
        if not keyword:
            raise RuntimeError("뉴스 수집 및 키워드 수집 모두 실패")
        from services.blog_naver_service import _detect_category as _naver_cat
        category = _naver_cat(keyword)
        _kw, _cat = keyword, category
        post_data = await _run_with_retry(lambda: generate_blog_post(_kw, _cat))
        post_data["keyword"] = keyword

    title = post_data["title"]
    summary = post_data.get("summary", [])
    tags = post_data.get("tags", [])
    meta_description = post_data.get("meta_description", "")

    # 요약 박스 → 광고 → 내부 링크 순서로 content 구성
    content = _inject_summary_box(post_data["content"], summary)
    content = _inject_adsterra_banner(content)
    content = _inject_adsterra_banner_mobile(content)
    content = _inject_adsterra_native(content)
    content = _inject_adsterra_social_bar(content)
    content = _inject_adsense(content)
    from services.blog_db import get_related_posts
    content = _inject_internal_links(content, get_related_posts(keyword, category, limit=3))

    # 3. 대표 이미지 생성 — 인라인 삽입 + WordPress Featured Image 설정
    featured_image_id: int | None = None
    img_url = ""
    try:
        img_result = await generate_post_image(keyword, title, category)
        if img_result:
            img_bytes, img_mime = img_result
            ext = "jpg" if "jpeg" in img_mime else img_mime.split("/")[-1]
            filename = f"post-{chosen_slot}-{datetime.now(KST).strftime('%Y%m%d%H%M%S')}.{ext}"
            img_upload = await upload_image(img_bytes, img_mime, filename)
            img_url = img_upload.get("url", "")
            raw_id = img_upload.get("id")
            if raw_id:
                try:
                    featured_image_id = int(raw_id)
                except (ValueError, TypeError):
                    pass
                logger.info("Post image uploaded (featured only) — url=%s, attachment_id=%s", img_url, featured_image_id)
    except Exception as e:
        logger.warning("Image step failed, continuing without image: %s", e)

    # 5. WordPress 발행
    try:
        wp_result = await _run_with_retry(
            lambda: publish_post(
                title=title,
                content=content,
                tags=tags,
                category=category,
                meta_description=meta_description,
                status=wp_status,
                featured_image_id=featured_image_id,
            )
        )
    except Exception as e:
        save_post_log(keyword, category, title, status="FAILED", error_msg=str(e))
        logger.error("WordPress publish failed: %s", e)
        raise

    wp_post_id = wp_result["post_id"]
    wp_url = wp_result["url"]

    # 5-1. JSON-LD + 트래킹 픽셀 삽입 (발행 후 post_id 확보 후 편집)
    api_base = os.getenv("API_BASE_URL", "").rstrip("/")
    if api_base:
        json_ld = _inject_json_ld(title, meta_description, wp_url, lang="ko", image_url=img_url, tags=tags)
        tracking_pixel = (
            f'\n<img src="{api_base}/blog/track/{wp_post_id}" '
            f'width="1" height="1" style="display:none" alt="">'
        )
        try:
            await edit_post(wp_post_id, {"post_content": json_ld + content + tracking_pixel})
            logger.info("JSON-LD + tracking pixel injected — post_id=%d", wp_post_id)
        except Exception as e:
            logger.warning("Post metadata inject failed: %s", e)

    # 6. 로그 저장
    mark_keyword_used(keyword, category)
    save_post_log(
        keyword=keyword,
        topic=category,
        title=title,
        wp_post_id=wp_post_id,
        wp_url=wp_url,
        status=wp_result["status"],
    )

    # 7. 검색엔진 핑
    try:
        from services.blog_ping_service import ping_search_engines
        await ping_search_engines(wp_url)
    except Exception as e:
        logger.warning("Search engine ping failed: %s", e)

    result = {
        "slot": chosen_slot,
        "category": category,
        "keyword": keyword,
        "title": title,
        "tags": tags,
        "meta_description": meta_description,
        "wp_post_id": wp_post_id,
        "wp_url": wp_url,
        "wp_status": wp_result["status"],
    }
    logger.info("=== Blog pipeline done — post_id=%s ===", wp_post_id)
    return result


_EN_QUALITY_THRESHOLD = float(os.getenv("EN_QUALITY_THRESHOLD", "85"))
_EN_QUALITY_MAX_ATTEMPTS = 3  # 1 initial + 2 regenerations
_en_hub_pipeline_lock = asyncio.Lock()


async def run_en_guide_pipeline(wp_status: str = "publish") -> dict:
    """Hub-and-Spoke 영어 SEO 포스트 파이프라인.

    두 실행이 겹치면(예: 재시작 catch-up과 수동 트리거가 거의 동시에 도는 경우) 서로
    상대방의 DB 저장을 못 본 채 같은 허브의 같은 서브토픽을 동시에 골라 중복 발행하는
    레이스가 생긴다 — 락으로 전체 파이프라인을 직렬화해 방지한다.
    """
    async with _en_hub_pipeline_lock:
        return await _run_en_guide_pipeline_locked(wp_status)


async def _run_en_guide_pipeline_locked(wp_status: str = "publish") -> dict:
    init_db()
    logger.info("=== EN hub pipeline start ===")

    from services.blog_db import get_diversity_data, get_category_related_posts
    from services.blog_topic_engine import (
        pick_topic_en_hub, TOPIC_RECENT_HISTORY_DAYS, TOPIC_RECENT_HISTORY_LIMIT, _too_similar,
    )
    from services.blog_gemini_service import generate_english_hub_post, score_post_quality

    # platform+language로 필터링 — 네이버/트렌드랩 등 다른 파이프라인 글이 섞이면
    # 최근 이력이 희석되어 반복 방지가 무력화된다 (하루 몇 번뿐인 영어 가이드 이력이
    # 하루 여러 번 도는 다른 파이프라인 글에 묻혀 버림).
    diversity = get_diversity_data(
        days=TOPIC_RECENT_HISTORY_DAYS, platform="wordpress", language="en",
        limit=TOPIC_RECENT_HISTORY_LIMIT,
    )
    used_kw = diversity["keywords"]
    recent_titles = diversity["titles"]

    picked = await pick_topic_en_hub(exclude_keywords=used_kw, recent_titles=recent_titles)
    keyword, category = picked["keyword"], picked["category"]
    hub, subtopic_key, intent = picked["hub"], picked["subtopic_key"], picked["intent"]

    related = get_category_related_posts(f"{category}_en", limit=5)  # 같은 카테고리 글(허브 무관) — 이번 실행 내내 고정

    best_post_data, best_quality, best_avg = None, None, -1.0
    for attempt in range(1, _EN_QUALITY_MAX_ATTEMPTS + 1):
        post_data = await _run_with_retry(
            lambda: generate_english_hub_post(keyword=keyword, category=category, hub=hub, intent=intent)
        )
        title = post_data["title"]
        is_dup = _too_similar(title, recent_titles)
        quality = await score_post_quality(
            keyword=keyword, intent=intent, hub=hub, title=title,
            meta_description=post_data.get("meta_description", ""), content=post_data["content"],
        )
        passed = quality["average"] >= _EN_QUALITY_THRESHOLD and not is_dup
        logger.info(
            "EN hub quality attempt %d/%d — avg=%.1f dup=%s pass=%s reason=%s",
            attempt, _EN_QUALITY_MAX_ATTEMPTS, quality["average"], is_dup, passed, quality.get("reason", ""),
        )
        if quality["average"] > best_avg:
            best_post_data, best_quality, best_avg = post_data, quality, quality["average"]
        if passed:
            break
    else:
        logger.warning(
            "EN hub quality gate: no attempt passed threshold=%.0f, publishing best attempt (avg=%.1f)",
            _EN_QUALITY_THRESHOLD, best_avg,
        )

    post_data, quality = best_post_data, best_quality
    title = post_data["title"]
    summary = post_data.get("summary", [])
    tags = post_data.get("tags", [])
    meta_description = post_data.get("meta_description", "")

    content = _inject_summary_box(post_data["content"], summary, lang="en")
    content = _inject_adsterra_banner(content)
    content = _inject_adsterra_banner_mobile(content)
    content = _inject_adsterra_native(content)
    content = _inject_adsterra_social_bar(content)
    content = _inject_adsense(content)
    content = _inject_internal_links(content, related, lang="en")  # 허브 형제글 전용

    # 이미지
    featured_image_id: int | None = None
    img_url = ""
    try:
        img_result = await generate_post_image(keyword, title, category, summary=summary, hub=hub)
        if img_result:
            img_bytes, img_mime = img_result
            ext = "jpg" if "jpeg" in img_mime else img_mime.split("/")[-1]
            filename = f"post-en-{datetime.now(KST).strftime('%Y%m%d%H%M%S')}.{ext}"
            img_upload = await upload_image(img_bytes, img_mime, filename)
            img_url = img_upload.get("url", "")
            raw_id = img_upload.get("id")
            if raw_id:
                try:
                    featured_image_id = int(raw_id)
                except (ValueError, TypeError):
                    pass
    except Exception as e:
        logger.warning("EN hub image step failed: %s", e)

    # WordPress 발행
    try:
        wp_result = await _run_with_retry(
            lambda: publish_post(
                title=title,
                content=content,
                tags=tags,
                category=category,
                meta_description=meta_description,
                status=wp_status,
                featured_image_id=featured_image_id,
            )
        )
    except Exception as e:
        save_post_log(
            keyword, f"{category}_en", title, status="FAILED", error_msg=str(e), language="en",
            hub=hub, subtopic_key=subtopic_key, intent=intent,
        )
        logger.error("EN hub WordPress publish failed: %s", e)
        raise

    wp_post_id = wp_result["post_id"]
    wp_url = wp_result["url"]

    api_base = os.getenv("API_BASE_URL", "").rstrip("/")
    if api_base:
        json_ld = _inject_json_ld(title, meta_description, wp_url, lang="en", image_url=img_url, tags=tags)
        tracking_pixel = (
            f'\n<img src="{api_base}/blog/track/{wp_post_id}" '
            f'width="1" height="1" style="display:none" alt="">'
        )
        try:
            await edit_post(wp_post_id, {"post_content": json_ld + content + tracking_pixel})
        except Exception as e:
            logger.warning("EN hub metadata inject failed: %s", e)

    save_post_log(
        keyword=keyword, topic=f"{category}_en", title=title,
        wp_post_id=wp_post_id, wp_url=wp_url, status=wp_result["status"],
        language="en", hub=hub, subtopic_key=subtopic_key, intent=intent,
    )
    try:
        from services.blog_ping_service import ping_search_engines
        await ping_search_engines(wp_url)
    except Exception as e:
        logger.warning("Search engine ping failed: %s", e)
    logger.info(
        "=== EN hub pipeline done — post_id=%s hub=%s subtopic=%s quality=%.1f ===",
        wp_post_id, hub, subtopic_key, quality["average"],
    )
    return {
        "slot": "evening",
        "lang": "en",
        "category": category,
        "hub": hub,
        "subtopic_key": subtopic_key,
        "intent": intent,
        "keyword": keyword,
        "title": title,
        "tags": tags,
        "quality_score": quality["average"],
        "wp_post_id": wp_post_id,
        "wp_url": wp_url,
        "wp_status": wp_result["status"],
    }


async def run_naver_ready_pipeline(
    slot: str = "morning",
    forced_keyword: str | None = None,
    forced_category: str | None = None,
    forced_intent: str | None = None,
) -> dict:
    """네이버 블로그 READY 콘텐츠 생성 → DB 저장 (수동 발행용)."""
    init_db()
    logger.info("=== Naver READY pipeline start — slot=%s ===", slot)

    from services.blog_topic_engine import pick_topic_naver

    if forced_keyword:
        topic_kw = forced_keyword
        topic_cat = forced_category or "이슈"
        topic_intent = forced_intent or "Hot"
        logger.info("Naver READY forced keyword: '%s'", topic_kw)
    else:
        # 뉴스 pre-check: 기사 부족하면 최대 3회 재시도
        from services.naver_api import search_news
        import re as _re

        for _attempt in range(3):
            topic_kw, topic_cat, topic_intent = await pick_topic_naver()
            # 엔티티 추출 후 뉴스 확인 (첫 단어 or 전체 키워드 기사 수)
            _entity = topic_kw.split()[0] if " " in topic_kw else topic_kw
            try:
                _arts = await search_news(_entity, display=5, sort="date")
                _art_count = len(_arts) if isinstance(_arts, list) else 0
            except Exception:
                _art_count = 5  # 검색 실패 시 통과
            if _art_count >= 2:
                logger.info("Naver topic OK: '%s' (news=%d, attempt=%d)", topic_kw, _art_count, _attempt + 1)
                break
            logger.warning("Naver topic retry (news=%d): '%s'", _art_count, topic_kw)
        else:
            logger.warning("Naver topic: all 3 attempts had low news coverage, proceeding anyway")

    from content_engine.naver.generator import NaverKRGenerator
    gen = NaverKRGenerator()
    _kw, _cat, _int = topic_kw, topic_cat, topic_intent
    generated = await _run_with_retry(lambda: gen.generate(_kw, _cat, intent=_int))

    # 썸네일 이미지 생성 시도
    thumbnail_url: str | None = None
    try:
        img_result = await generate_post_image(generated.keyword, generated.title, generated.category, summary=generated.summary)
        if img_result:
            img_bytes, img_mime = img_result
            ext = "jpg" if "jpeg" in img_mime else img_mime.split("/")[-1]
            filename = f"naver-{slot}-{datetime.now(KST).strftime('%Y%m%d%H%M%S')}.{ext}"
            img_upload = await upload_image(img_bytes, img_mime, filename)
            thumbnail_url = img_upload.get("url")
    except Exception as e:
        logger.warning("Naver image step failed: %s", e)

    scores = _calc_scores(
        title=generated.title,
        content=generated.content,
        category=generated.category,
        gemini_score=generated.gemini_traffic_score,
    )
    logger.info(
        "Naver scores — traffic=%s ctr=%s seo=%s ai_risk=%s dopamine=%s",
        scores["traffic_score"], scores["ctr_score"], scores["seo_score"],
        scores["ai_risk"], scores["dopamine_score"],
    )

    from publisher.naver.publisher import NaverPublisher
    from publisher.base import PostPayload
    pub = NaverPublisher()
    result = await pub.publish(PostPayload(
        keyword=generated.keyword,
        topic=generated.category,
        title=generated.title,
        content=generated.content,
        tags=generated.tags,
        meta_description=generated.meta_description,
        summary=generated.summary,
        language="ko",
        thumbnail_url=thumbnail_url,
        traffic_score=scores["traffic_score"],
        competition=generated.competition,
        trend_level=generated.trend_level,
        score_reason=generated.score_reason,
        ctr_score=scores["ctr_score"],
        seo_score=scores["seo_score"],
        ai_risk=scores["ai_risk"],
        dopamine_score=scores["dopamine_score"],
    ))

    mark_keyword_used(generated.keyword, generated.category)
    logger.info("=== Naver READY pipeline done — id=%s ===", result.post_id)
    return {
        "slot": slot,
        "platform": "naver",
        "status": result.status,
        "db_id": result.post_id,
        "keyword": generated.keyword,
        "category": generated.category,
        "title": generated.title,
        "tags": generated.tags,
        "thumbnail_url": thumbnail_url,
    }


async def run_kr_guide_pipeline(slot: str = "morning", wp_status: str = "publish") -> dict:
    """07시/13시 한국어 검색형 가이드 파이프라인. Topic Engine으로 동적 키워드 선정."""
    init_db()
    logger.info("=== KR guide pipeline start — slot=%s ===", slot)

    from services.blog_db import get_diversity_data
    from services.blog_topic_engine import (
        pick_topic, TOPIC_RECENT_HISTORY_DAYS, TOPIC_RECENT_HISTORY_LIMIT,
    )
    import pytz as _pytz
    _kst = _pytz.timezone("Asia/Seoul")
    _today = datetime.now(_kst).strftime("%Y-%m-%d")
    # platform+language로 필터링 — 네이버/트렌드랩 글이 섞이면 최근 이력이 희석된다
    diversity = get_diversity_data(
        days=TOPIC_RECENT_HISTORY_DAYS, platform="wordpress", language="ko",
        limit=TOPIC_RECENT_HISTORY_LIMIT,
    )
    used_kw = diversity["keywords"]
    today_cats = {
        cat for cat, created in zip(diversity["categories"], diversity["created_at"])
        if created.startswith(_today)
    }

    topic_kw, topic_cat = await pick_topic(
        lang="ko",
        exclude_topics=used_kw,
        exclude_categories=today_cats,
        platform="wordpress",
    )

    post_data = await _run_with_retry(
        lambda: generate_kr_guide_post(
            used_kw,
            exclude_categories=today_cats,
            forced_keyword=topic_kw,
            forced_category=topic_cat,
        )
    )
    category = post_data.pop("_category", "AI")
    keyword = post_data["keyword"]
    title = post_data["title"]
    summary = post_data.get("summary", [])
    tags = post_data.get("tags", [])
    meta_description = post_data.get("meta_description", "")

    content = _inject_summary_box(post_data["content"], summary)
    content = _inject_adsterra_banner(content)
    content = _inject_adsterra_banner_mobile(content)
    content = _inject_adsterra_native(content)
    content = _inject_adsterra_social_bar(content)
    content = _inject_adsense(content)
    from services.blog_db import get_related_posts
    content = _inject_internal_links(content, get_related_posts(keyword, category, limit=3))

    featured_image_id: int | None = None
    img_url = ""
    try:
        img_result = await generate_post_image(keyword, title, category)
        if img_result:
            img_bytes, img_mime = img_result
            ext = "jpg" if "jpeg" in img_mime else img_mime.split("/")[-1]
            filename = f"post-kr-{slot}-{datetime.now(KST).strftime('%Y%m%d%H%M%S')}.{ext}"
            img_upload = await upload_image(img_bytes, img_mime, filename)
            img_url = img_upload.get("url", "")
            raw_id = img_upload.get("id")
            if raw_id:
                try:
                    featured_image_id = int(raw_id)
                except (ValueError, TypeError):
                    pass
    except Exception as e:
        logger.warning("KR guide image step failed: %s", e)

    try:
        wp_result = await _run_with_retry(
            lambda: publish_post(
                title=title,
                content=content,
                tags=tags,
                category=category,
                meta_description=meta_description,
                status=wp_status,
                featured_image_id=featured_image_id,
            )
        )
    except Exception as e:
        save_post_log(keyword, category, title, status="FAILED", error_msg=str(e))
        logger.error("KR guide WordPress publish failed: %s", e)
        raise

    wp_post_id = wp_result["post_id"]
    wp_url = wp_result["url"]

    api_base = os.getenv("API_BASE_URL", "").rstrip("/")
    if api_base:
        json_ld = _inject_json_ld(title, meta_description, wp_url, lang="ko", image_url=img_url, tags=tags)
        tracking_pixel = (
            f'\n<img src="{api_base}/blog/track/{wp_post_id}" '
            f'width="1" height="1" style="display:none" alt="">'
        )
        try:
            await edit_post(wp_post_id, {"post_content": json_ld + content + tracking_pixel})
        except Exception as e:
            logger.warning("KR guide metadata inject failed: %s", e)

    mark_keyword_used(keyword, category)
    save_post_log(
        keyword=keyword, topic=category, title=title,
        wp_post_id=wp_post_id, wp_url=wp_url, status=wp_result["status"],
    )
    try:
        from services.blog_ping_service import ping_search_engines
        await ping_search_engines(wp_url)
    except Exception as e:
        logger.warning("Search engine ping failed: %s", e)
    logger.info("=== KR guide pipeline done — slot=%s post_id=%s ===", slot, wp_post_id)
    return {
        "slot": slot,
        "lang": "ko",
        "category": category,
        "keyword": keyword,
        "title": title,
        "tags": tags,
        "wp_post_id": wp_post_id,
        "wp_url": wp_url,
        "wp_status": wp_result["status"],
    }
