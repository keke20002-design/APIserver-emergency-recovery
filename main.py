import asyncio
import hashlib
import json
import logging
import os
import random

import httpx
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
import pytz
from dotenv import load_dotenv
load_dotenv()
from fastapi import Body, Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response

from services.blog_pipeline import run_blog_pipeline, run_en_guide_pipeline, run_kr_guide_pipeline, run_naver_ready_pipeline
from services.trader_pipeline import run_trader_pipeline
from services.blog_db import get_recent_posts, get_used_keywords, init_db as init_blog_db
from services.blog_wordpress_service import update_additional_css, delete_post as wp_delete_post, edit_post as wp_edit_post, setup_branding as wp_setup_branding, setup_nav_menu as wp_setup_nav_menu, inject_cat_pagination_fix as wp_inject_cat_pagination_fix, patch_all_posts_pagination as wp_patch_all_posts_pagination
from services.analyzer import analyze
from services.kbo_schedule import fetch_kbo_postseason, fetch_kbo_schedule
from services.stadium_updater import run_collection
from services.supabase_service import (
    get_pending_updates, approve_update, reject_update,
    get_pending_team_news, get_approved_team_news, approve_team_news, reject_team_news,
    save_team_news,
)
from services.team_news_crawler import crawl_all_team_news
from services.sports_news_service import fetch_match_sports_news
# from services.community_crawler import collect_community_posts  # 커뮤니티 요약 보류
from services.gemini_service import (
    analyze_image as gemini_analyze,
    summarize_recipe as gemini_summarize_recipe,
    get_ingredient_substitute as gemini_ingredient_substitute,
    generate_artwork_title,
    generate_artwork_poem,
    generate_artwork_analysis,
    generate_artwork_title_from_bytes,
    generate_artwork_poem_from_bytes,
    generate_artwork_analysis_from_bytes,
    generate_game_insight,
    analyze_bible_topics,
    translate_titles as gemini_translate_titles,
)  # summarize_community 보류
from services.speech_service import generate_speech_feedback
from services.purchase_service import (
    consume_credit as consume_coaching_credit,
    get_status as get_purchase_status,
    verify_and_grant_purchase,
    verify_id_token,
)
from services.kbo_standings import fetch_kbo_standings
from services.kleague_schedule import fetch_kleague_schedule, fetch_kleague_upcoming
from services.ewc_schedule import fetch_ewc_nearby, fetch_ewc_schedule, fetch_ewc_standings
from services.lck_schedule import fetch_lck_nearby, fetch_lck_schedule
from services.lck_team_detail import fetch_lck_lineup, fetch_lck_team_detail
from services.lck_teams import LCK_TEAMS
from services.restaurant import search_restaurants
from services.youtube_service import search_cheer_video, search_ewc_videos, search_international_videos, search_kbo_videos,search_lck_videos, search_epl_videos, search_recipe_videos, search_bible_videos
from services.naver_video_service import fetch_naver_lck_replay
from services.riot_service import get_puuid_by_riot_id, get_summoner_by_puuid
from services.epl_schedule import fetch_epl_schedule, fetch_epl_nearby, fetch_team_last_formation, fetch_team_upcoming_fixtures
from services.epl_standings import fetch_epl_standings
from services.epl_match_stats import (
    fetch_season_team_stats, compute_prediction, build_key_stats, build_form, MODEL_VERSION,
)
from services.api_football_odds import fetch_match_odds, compute_market_probability
from services.supabase_service import (
    upsert_match_odds, get_match_odds,
    upsert_prediction, get_prediction,
    upsert_model_vs_market,
)
from services.epl_tactical_battle import (
    build_match_context, generate_tactical_battle, _context_signature, PROMPT_VERSION,
)
from services.epl_tactical_stories import (
    generate_tactical_stories, PROMPT_VERSION as STORIES_PROMPT_VERSION,
)
from pydantic import BaseModel
from services.worldcup_schedule import (
    fetch_worldcup_schedule, fetch_worldcup_standings,
    fetch_worldcup_bracket, WORLDCUP_STADIUMS,
)
from services.worldcup_insight import generate_worldcup_match_insight, generate_worldcup_win_prediction
from services.dashboard_router import (
    router as dashboard_router,
    run_dashboard_sync_job,
    dashboard_sync_already_ran_today,
)
from services.localcur_router import admin_router as localcur_admin_router
from services.localcur_router import router as localcur_router
from services import schedule_sync_job
from services.annual_leave_router import router as annual_leave_router, deliver_notifications as annual_leave_push, daily_accrual as annual_leave_accrual
from services.kfood_trends import (
    admin_router as kfood_trends_admin_router,
    public_router as kfood_trends_public_router,
    run_weekly_collection as run_kfood_trends_collection,
)
from utils.cache import cache

# 성경 구절 데이터 (서버 시작 시 1회 로드)
_BIBLE_DATA: list[dict] = []

def _load_bible_data():
    global _BIBLE_DATA
    bible_path = Path(__file__).parent / "data" / "bible.json"
    try:
        with open(bible_path, encoding="utf-8") as f:
            _BIBLE_DATA = json.load(f)
        logger_bootstrap = logging.getLogger(__name__)
        logger_bootstrap.info("Loaded %d bible verses", len(_BIBLE_DATA))
    except Exception as e:
        logging.getLogger(__name__).error("Failed to load bible.json: %s", e)


# 구장별 팀 (기본 순서 — 오늘 홈팀 없으면 이 순서 그대로)
STADIUM_TEAMS: dict[str, list[str]] = {
    "잠실": ["LG트윈스", "두산베어스"],
    "고척": ["키움히어로즈"],
    "문학": ["SSG랜더스"],
    "수원": ["KT위즈"],
    "사직": ["롯데자이언츠"],
    "대전": ["한화이글스"],
    "대구": ["삼성라이온즈"],
    "광주": ["KIA타이거즈"],
    "창원": ["NC다이노스"],
}

# API에서 오는 팀 약칭 → 전체 팀명
TEAM_FULL_NAME: dict[str, str] = {
    "LG":   "LG트윈스",
    "두산": "두산베어스",
    "키움": "키움히어로즈",
    "SSG":  "SSG랜더스",
    "KT":   "KT위즈",
    "롯데": "롯데자이언츠",
    "한화": "한화이글스",
    "삼성": "삼성라이온즈",
    "KIA":  "KIA타이거즈",
    "NC":   "NC다이노스",
}

_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

_file_handler = TimedRotatingFileHandler(
    "api_server.log",
    when="midnight",
    backupCount=7,
    encoding="utf-8",
)
_file_handler.setFormatter(_fmt)

_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_fmt)

logging.basicConfig(level=logging.INFO, handlers=[_file_handler, _console_handler])
logger = logging.getLogger(__name__)

# Locations to pre-fetch on schedule
KST = pytz.timezone("Asia/Seoul")


def _require_admin(
    admin_id: str = Body(..., embed=True),
    admin_pw: str = Body(..., embed=True),
):
    if admin_id != os.getenv("BLOG_ADMIN_ID", "") or admin_pw != os.getenv("BLOG_ADMIN_PW", ""):
        raise HTTPException(status_code=403, detail="관리자 인증 실패")


POPULAR_LOCATIONS = ["잠실", "고척", "문학", "수원", "사직", "대전", "대구", "광주", "창원"]

scheduler = AsyncIOScheduler()
_schedule_lock = asyncio.Lock()
_catchup_running = False


# --- 커뮤니티 요약 보류 (나중에 활성화) ---
# _COMMUNITY_SUMMARY_KEY = "community_summary:lines"
# _COMMUNITY_SUMMARY_TTL = 700
#
# async def refresh_community_summary():
#     try:
#         posts = await collect_community_posts(max_per_site=15)
#         if not posts:
#             return
#         lines = await summarize_community(posts)
#         if lines:
#             cache.set(_COMMUNITY_SUMMARY_KEY, lines, ttl=_COMMUNITY_SUMMARY_TTL)
#     except Exception as e:
#         logger.error("community_summary refresh failed: %s", e)
# --- 여기까지 ---


async def collect_team_news():
    """Background job: 각 구단 홈페이지 당일 공지/이벤트 수집 → Supabase pending 저장."""
    try:
        items = await crawl_all_team_news()
        saved = 0
        for item in items:
            if await save_team_news(item):
                saved += 1
        logger.info("Team news collection done: %d saved / %d total", saved, len(items))
    except Exception as e:
        logger.error("Team news collection failed: %s", e)


async def collect_stadium_info():
    """Background job: 야구장 최신 정보 수집 → Supabase 저장 (pending 상태)."""
    try:
        result = await run_collection()
        logger.info("Stadium info collection: %s", result)
    except Exception as e:
        logger.error("Stadium info collection failed: %s", e)


async def schedule_pregame_collections():
    """오늘 KBO 경기 일정을 확인해 경기 시작 2시간 전 수집 잡을 등록."""
    try:
        games = await fetch_kbo_schedule()
        registered = 0
        for game in games:
            time_str = game.get("time", "")           # "HH:MM"
            stadium_name = game.get("stadium", "")    # 예: "잠실종합운동장"
            if not time_str or not stadium_name:
                continue

            # 구장 이름에서 단축키 매핑
            stadium_key = next(
                (k for k in STADIUM_TEAMS if k in stadium_name), None
            )
            if not stadium_key:
                continue

            try:
                today = date.today()
                game_dt = datetime.strptime(f"{today} {time_str}", "%Y-%m-%d %H:%M")
                trigger_dt = game_dt - timedelta(hours=2)
                if trigger_dt <= datetime.now():
                    continue

                job_id = f"pregame_{stadium_key}_{time_str.replace(':', '')}"
                if not scheduler.get_job(job_id):
                    scheduler.add_job(
                        collect_stadium_info,
                        trigger="date",
                        run_date=trigger_dt,
                        id=job_id,
                    )
                    logger.info("Scheduled pre-game collection: %s at %s", stadium_key, trigger_dt)
                    registered += 1
            except Exception as e:
                logger.warning("Failed to schedule pre-game job for %s: %s", stadium_name, e)

        logger.info("Pre-game collection jobs registered: %d", registered)
    except Exception as e:
        logger.error("Pre-game schedule setup failed: %s", e)


async def prefetch_popular_locations():
    """Background job: pre-fetch parking data for popular locations."""
    for location in POPULAR_LOCATIONS:
        try:
            result = await analyze(location)
            cache.set(location, result)
            logger.info("Pre-fetched parking data for %s", location)
        except Exception as e:
            logger.error("Failed to pre-fetch %s: %s", location, e)


_kleague_lock = asyncio.Lock()
_lck_lock = asyncio.Lock()
_ewc_lock = asyncio.Lock()
_worldcup_lock = asyncio.Lock()


def _save_lineup_if_finished(games: list[dict]) -> None:
    """경기 종료된 게임의 타자 라인업을 data/lineups/{팀}.json 에 저장."""
    lineups_dir = Path(__file__).parent / "data" / "lineups"
    lineups_dir.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()

    for game in games:
        if game.get("status") != "경기 종료":
            continue
        for side in ("home", "away"):
            team = game.get(f"{side}_team", "")
            lineup_raw = game.get(f"{side}_lineup") or []
            if not team or not lineup_raw:
                continue
            # 선발 9명만: 같은 타순은 첫 번째(선발) 선수만 유지
            seen_orders: set[int] = set()
            batters = []
            for b in lineup_raw:
                if not b.get("name"):
                    continue
                order = int(b.get("order") or 0)
                if order < 1 or order > 9 or order in seen_orders:
                    continue
                seen_orders.add(order)
                # 교체 기록 포지션("유二", "우좌" 등) → 첫 글자만 사용
                pos_raw = str(b.get("position") or "")
                pos = pos_raw[0] if pos_raw else ""
                batters.append({
                    "order": order,
                    "name": b.get("name", ""),
                    "position": pos,
                    "season_avg": b.get("season_avg"),
                    "season_hr": b.get("season_hr"),
                    "season_rbi": b.get("season_rbi"),
                })
            if not batters:
                continue
            path = lineups_dir / f"{team}.json"
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({"team": team, "date": today, "batters": batters}, f, ensure_ascii=False, indent=2)
                logger.info("Saved lineup for %s (%d batters)", team, len(batters))
            except Exception as e:
                logger.error("Failed to save lineup for %s: %s", team, e)


async def prefetch_game_schedule():
    """Background job: pre-fetch today's KBO game schedule."""
    if _schedule_lock.locked():
        logger.info("Schedule prefetch already running, skipping")
        return
    async with _schedule_lock:
        try:
            games = await fetch_kbo_schedule()
            result = {
                "date": date.today().isoformat(),
                "total_games": len(games),
                "games": games,
            }
            live = any(g.get("status") == "경기 중" for g in games)
            interval = 1 if live else 10
            ttl = 70 if live else 600
            cache.set("games:today", result, ttl=ttl)
            scheduler.reschedule_job("game_schedule", trigger="interval", minutes=interval)
            logger.info("Pre-fetched KBO schedule: %d games, next poll in %dmin (ttl=%ds)", len(games), interval, ttl)
            _save_lineup_if_finished(games)
            # 오늘 경기 없으면 어제 경기 pre-fetch (홈화면 fallback 캐시)
            if not games:
                yesterday = date.today() - timedelta(days=1)
                yesterday_key = f"games:{yesterday.isoformat()}"
                if not cache.get(yesterday_key):
                    try:
                        prev_games = await fetch_kbo_schedule(yesterday)
                        prev_result = {
                            "date": yesterday.isoformat(),
                            "total_games": len(prev_games),
                            "games": prev_games,
                        }
                        cache.set(yesterday_key, prev_result, ttl=86400)
                        logger.info("Pre-fetched yesterday KBO schedule: %d games", len(prev_games))
                    except Exception as e2:
                        logger.error("Failed to pre-fetch yesterday KBO schedule: %s", e2)
        except Exception as e:
            logger.error("Failed to pre-fetch KBO schedule: %s", e)


async def _rebuild_lineups_if_missing() -> None:
    """lineup 파일이 없는 팀이 있으면 최근 며칠치 경기를 역순으로 조회해 재생성."""
    lineups_dir = Path(__file__).parent / "data" / "lineups"
    lineups_dir.mkdir(parents=True, exist_ok=True)
    kbo_teams = {"LG", "두산", "KIA", "SSG", "KT", "NC", "삼성", "한화", "롯데", "키움"}
    missing = kbo_teams - {p.stem for p in lineups_dir.glob("*.json")}
    if not missing:
        return
    logger.info("Missing lineup files for: %s — rebuilding from recent games", missing)
    for days_ago in range(1, 8):
        if not missing:
            break
        target = date.today() - timedelta(days=days_ago)
        try:
            past_games = await fetch_kbo_schedule(target)
            _save_lineup_if_finished(past_games)
            still_missing = kbo_teams - {p.stem for p in lineups_dir.glob("*.json")}
            recovered = missing - still_missing
            if recovered:
                logger.info("Recovered lineups for %s from %s", recovered, target.isoformat())
            missing = still_missing
        except Exception as e:
            logger.error("Failed to rebuild lineups from %s: %s", target.isoformat(), e)
    if missing:
        logger.warning("Could not recover lineups for: %s", missing)


async def prefetch_kleague_schedule():
    """Background job: pre-fetch today's K리그1 schedule."""
    if _kleague_lock.locked():
        logger.info("K리그 prefetch already running, skipping")
        return
    async with _kleague_lock:
        try:
            games = await fetch_kleague_schedule()
            result = {
                "date": date.today().isoformat(),
                "total_games": len(games),
                "games": games,
            }
            live = any(g.get("status") in ("경기 중", "하프타임") for g in games)
            interval = 1 if live else 10
            ttl = 70 if live else 600
            cache.set("kleague:today", result, ttl=ttl)
            scheduler.reschedule_job("kleague_schedule", trigger="interval", minutes=interval)
            logger.info("Pre-fetched K리그 schedule: %d games, next poll in %dmin (ttl=%ds)", len(games), interval, ttl)
        except Exception as e:
            logger.error("Failed to pre-fetch K리그 schedule: %s", e)


async def prefetch_worldcup_schedule():
    """Background job: pre-fetch today's FIFA 월드컵 schedule."""
    if _worldcup_lock.locked():
        logger.info("월드컵 prefetch already running, skipping")
        return
    async with _worldcup_lock:
        try:
            games = await fetch_worldcup_schedule()
            result = {
                "date": date.today().isoformat(),
                "total_games": len(games),
                "games": games,
            }
            live = any(g.get("status") in ("경기 중", "하프타임") for g in games)
            interval = 1 if live else 10
            ttl = 70 if live else 600
            cache.set("worldcup:today", result, ttl=ttl)
            scheduler.reschedule_job("worldcup_schedule", trigger="interval", minutes=interval)
            logger.info("Pre-fetched 월드컵 schedule: %d games, next poll in %dmin (ttl=%ds)", len(games), interval, ttl)
        except Exception as e:
            logger.error("Failed to pre-fetch 월드컵 schedule: %s", e)


async def prefetch_lck_schedule():
    """Background job: pre-fetch today's LCK schedule."""
    if _lck_lock.locked():
        logger.info("LCK prefetch already running, skipping")
        return
    async with _lck_lock:
        try:
            games = await fetch_lck_schedule()
            result = {
                "date": date.today().isoformat(),
                "total_games": len(games),
                "games": games,
            }
            live = any(g.get("status") == "경기 중" for g in games)
            interval = 1 if live else 10
            ttl = 70 if live else 600
            cache.set("lck:today", result, ttl=ttl)
            scheduler.reschedule_job("lck_schedule", trigger="interval", minutes=interval)
            logger.info("Pre-fetched LCK schedule: %d games, next poll in %dmin (ttl=%ds)", len(games), interval, ttl)
        except Exception as e:
            logger.error("Failed to pre-fetch LCK schedule: %s", e)


async def prefetch_ewc_schedule():
    """Background job: pre-fetch today's EWC schedule."""
    if _ewc_lock.locked():
        logger.info("EWC prefetch already running, skipping")
        return
    async with _ewc_lock:
        try:
            games = await fetch_ewc_schedule()
            result = {
                "date": date.today().isoformat(),
                "total_games": len(games),
                "games": games,
            }
            live = any(g.get("status") == "경기 중" for g in games)
            interval = 1 if live else 30
            ttl = 70 if live else 1800
            cache.set("ewc:today", result, ttl=ttl)
            scheduler.reschedule_job("ewc_schedule", trigger="interval", minutes=interval)
            logger.info("Pre-fetched EWC schedule: %d games, next poll in %dmin (ttl=%ds)", len(games), interval, ttl)
        except Exception as e:
            logger.error("Failed to pre-fetch EWC schedule: %s", e)


async def _catchup_dashboard_sync():
    """서버 기동 시 오늘(KST) 아직 대시보드 동기화가 안 됐으면 한 번 실행."""
    await asyncio.sleep(5)  # 서버 완전 기동 대기
    try:
        if await dashboard_sync_already_ran_today():
            logger.info("Dashboard sync catch-up: 오늘 이미 동기화됨, 스킵")
            return
        await run_dashboard_sync_job()
    except Exception as e:
        logger.error("Dashboard sync catch-up failed: %s", e)


async def _catchup_blog_posts():
    """서버 재시작 시 오늘 놓친 블로그 슬롯을 순서대로 실행."""
    global _catchup_running
    if _catchup_running:
        logger.info("Blog catch-up: already running, skipping")
        return
    _catchup_running = True
    try:
        await _do_catchup_blog_posts()
    finally:
        _catchup_running = False


async def _do_catchup_blog_posts():
    from services.blog_db import get_recent_posts
    await asyncio.sleep(5)  # 서버 완전 기동 대기

    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    hour = now.hour

    # 오늘 발행된 슬롯 확인 (failed 제외)
    posts = get_recent_posts(limit=20)
    posted_today = [
        p for p in posts
        if p.get("created_at", "").startswith(today) and p.get("status") != "FAILED"
    ]
    posted_count = len(posted_today)

    # 각 슬롯 기준 시각 (KST): morning=7, afternoon=13, evening=19
    slots_to_run = []
    if hour >= 7 and posted_count == 0:
        slots_to_run.append("morning")
    if hour >= 13 and posted_count <= 1:
        slots_to_run.append("afternoon")
    if hour >= 19 and posted_count <= 2:
        slots_to_run.append("evening")

    if not slots_to_run:
        logger.info("Blog catch-up: no missed slots (posted_today=%d, hour=%d)", posted_count, hour)
        return

    logger.info("Blog catch-up: missed slots=%s (posted_today=%d, hour=%d)", slots_to_run, posted_count, hour)
    for slot in slots_to_run:
        await asyncio.sleep(2)
        if slot == "evening":
            await blog_post_evening(jitter=False)
        elif slot == "morning":
            await blog_post_en_midday(jitter=False)
        elif slot == "afternoon":
            await blog_post_afternoon(jitter=False)
        else:
            await _blog_post_job(slot)


async def _blog_post_job(slot: str):
    try:
        result = await run_blog_pipeline(slot=slot, wp_status="publish")
        logger.info("Blog post [%s] created: %s", slot, result.get("title"))
    except Exception as e:
        logger.error("Blog post [%s] failed: %s", slot, e)


async def blog_post_morning():
    try:
        result = await run_kr_guide_pipeline(slot="morning", wp_status="publish")
        logger.info("KR guide post [morning] created: %s", result.get("title"))
    except Exception as e:
        logger.error("KR guide post [morning] failed: %s", e)


EN_POST_JITTER_MAX_SECONDS = int(os.getenv("EN_POST_JITTER_MAX_MINUTES", "120")) * 60


async def blog_post_afternoon(jitter: bool = True):
    """13:00 영어 가이드 포스트 (EN SEO)."""
    if jitter:
        delay = random.randint(0, EN_POST_JITTER_MAX_SECONDS)
        logger.info("EN guide [afternoon] jitter: sleeping %ds", delay)
        await asyncio.sleep(delay)
    try:
        result = await run_en_guide_pipeline(wp_status="publish")
        logger.info("EN guide post [afternoon] created: %s", result.get("title"))
    except Exception as e:
        logger.error("EN guide post [afternoon] failed: %s", e)


async def blog_post_en_midday(jitter: bool = True):
    """07:00/10:00 영어 가이드 포스트 (EN SEO)."""
    if jitter:
        delay = random.randint(0, EN_POST_JITTER_MAX_SECONDS)
        logger.info("EN guide [midday] jitter: sleeping %ds", delay)
        await asyncio.sleep(delay)
    try:
        result = await run_en_guide_pipeline(wp_status="publish")
        logger.info("EN guide post [midday] created: %s", result.get("title"))
    except Exception as e:
        logger.error("EN guide post [midday] failed: %s", e)


async def blog_post_evening(jitter: bool = True):
    """19:00 영어 가이드 포스트 (EN SEO)."""
    if jitter:
        delay = random.randint(0, EN_POST_JITTER_MAX_SECONDS)
        logger.info("EN guide [evening] jitter: sleeping %ds", delay)
        await asyncio.sleep(delay)
    try:
        result = await run_en_guide_pipeline(wp_status="publish")
        logger.info("EN guide post created: %s", result.get("title"))
    except Exception as e:
        logger.error("EN guide post [evening] failed: %s", e)


async def trader_post_job():
    """08:00 한국 주식 분석 포스팅."""
    try:
        result = await run_trader_pipeline(wp_status="publish")
        logger.info("Trader post done: %s", result.get("title"))
    except FileNotFoundError:
        logger.warning("Trader post skipped: doc/trader.txt not found")
    except Exception as e:
        logger.error("Trader post failed: %s", e)


async def naver_ready_morning():
    """08:00 네이버 READY 콘텐츠 생성."""
    try:
        result = await run_naver_ready_pipeline(slot="morning")
        logger.info("Naver READY [morning] saved: %s", result.get("title"))
    except Exception as e:
        logger.error("Naver READY [morning] failed: %s", e)


async def naver_ready_midday():
    """12:00 네이버 READY 콘텐츠 생성."""
    try:
        result = await run_naver_ready_pipeline(slot="midday")
        logger.info("Naver READY [midday] saved: %s", result.get("title"))
    except Exception as e:
        logger.error("Naver READY [midday] failed: %s", e)


async def naver_ready_evening():
    """18:00 네이버 READY 콘텐츠 생성."""
    try:
        result = await run_naver_ready_pipeline(slot="evening")
        logger.info("Naver READY [evening] saved: %s", result.get("title"))
    except Exception as e:
        logger.error("Naver READY [evening] failed: %s", e)


async def naver_ready_night():
    """21:00 네이버 READY 콘텐츠 생성."""
    try:
        result = await run_naver_ready_pipeline(slot="night")
        logger.info("Naver READY [night] saved: %s", result.get("title"))
    except Exception as e:
        logger.error("Naver READY [night] failed: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    _load_bible_data()
    init_blog_db()
    scheduler.add_job(prefetch_popular_locations, "interval", minutes=5)
    scheduler.add_job(prefetch_game_schedule, "interval", minutes=10, id="game_schedule")
    scheduler.add_job(prefetch_kleague_schedule, "interval", minutes=10, id="kleague_schedule")
    scheduler.add_job(prefetch_worldcup_schedule, "interval", minutes=10, id="worldcup_schedule")
    scheduler.add_job(prefetch_lck_schedule, "interval", minutes=10, id="lck_schedule")
    scheduler.add_job(prefetch_ewc_schedule, "interval", minutes=30, id="ewc_schedule")
    # locakcurapp - 상품권 발행 공지 크롤링 + 지난 일정 마감 (매일 새벽 3시 KST)
    scheduler.add_job(
        schedule_sync_job.run_daily_sync,
        "cron",
        hour=3,
        minute=0,
        timezone="Asia/Seoul",
        id="localcur_schedule_sync",
    )
    # Dashboard - Google Ads/AdMob/GA4 동기화 (매일 새벽 2시 KST). 서버 기동 시
    # 오늘 아직 안 돌았으면 _catchup_dashboard_sync()가 별도로 즉시 한 번 더 실행한다.
    scheduler.add_job(
        run_dashboard_sync_job,
        "cron",
        hour=2,
        minute=0,
        timezone="Asia/Seoul",
        id="dashboard_daily_sync",
    )
    # EPL 시즌 종료 — 재개 시 주석 해제
    # scheduler.add_job(prefetch_epl_schedule, "interval", minutes=10, id="epl_schedule")
    # 커뮤니티 3줄 요약 — 보류 중
    # scheduler.add_job(refresh_community_summary, "cron",
    #     hour="17-23", minute="*/10", timezone="Asia/Seoul", id="community_summary_refresh")
    # 데이터 수집 잡 — 수동 실행 전까지 비활성화
    # scheduler.add_job(collect_team_news, "cron", hour=23, minute=0, id="team_news_daily")
    # scheduler.add_job(collect_stadium_info, "cron", hour=10, minute=0, id="stadium_info_10")
    # scheduler.add_job(collect_stadium_info, "cron", hour=15, minute=0, id="stadium_info_15")
    # scheduler.add_job(schedule_pregame_collections, "cron", hour=8, minute=0, id="pregame_scheduler")
    # 블로그 자동 포스팅 KST — 전부 영어 (EN 우선 전략) — 홀드 (사용자 요청, 2026-08-14, 재개 지시 전까지 주석 유지)
    # 07:00 EN / 10:00 EN / 13:00 EN / 19:00 EN
    # scheduler.add_job(blog_post_en_midday, "cron", hour=7,  minute=0, id="blog_morning",   timezone="Asia/Seoul")
    # scheduler.add_job(blog_post_en_midday, "cron", hour=10, minute=0, id="blog_en_midday", timezone="Asia/Seoul")
    # scheduler.add_job(blog_post_afternoon, "cron", hour=13, minute=0, id="blog_afternoon", timezone="Asia/Seoul")
    # scheduler.add_job(blog_post_evening,   "cron", hour=19, minute=0, id="blog_evening",   timezone="Asia/Seoul")
    # 네이버 READY 콘텐츠 생성: 08:00 / 12:00 / 18:00 / 21:00 KST — 홀드 (사용자 요청, 2026-08-14, 재개 지시 전까지 주석 유지)
    # scheduler.add_job(naver_ready_morning, "cron", hour=8,  minute=0, id="naver_morning", timezone="Asia/Seoul")
    # scheduler.add_job(naver_ready_midday,  "cron", hour=12, minute=0, id="naver_midday",  timezone="Asia/Seoul")
    # scheduler.add_job(naver_ready_evening, "cron", hour=18, minute=0, id="naver_evening", timezone="Asia/Seoul")
    # scheduler.add_job(naver_ready_night,   "cron", hour=21, minute=0, id="naver_night",   timezone="Asia/Seoul")
    scheduler.add_job(annual_leave_push, "interval", minutes=1, id="annual_leave_push", replace_existing=True, max_instances=1)
    scheduler.add_job(annual_leave_accrual, "cron", hour=0, minute=5, timezone=KST, id="annual_leave_accrual", replace_existing=True)
    scheduler.add_job(
        run_kfood_trends_collection,
        "cron",
        day_of_week="mon",
        hour=9,
        minute=0,
        timezone=KST,
        id="kfood_trends_weekly",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    scheduler.start()
    logger.info("Scheduler started: parking 5min, games 10min, kleague 10min, lck 10min, ewc 30min, stadium_info 10:00/15:00")
    # Run initial pre-fetch
    await prefetch_popular_locations()
    await prefetch_game_schedule()
    await _rebuild_lineups_if_missing()
    await prefetch_kleague_schedule()
    await prefetch_worldcup_schedule()
    await prefetch_lck_schedule()
    await prefetch_ewc_schedule()
    # EPL 시즌 종료 — 재개 시 주석 해제
    # await prefetch_epl_schedule()
    # 서버 시작 시 당일 경기 2시간 전 잡 등록 — 수동 실행 전까지 비활성화
    # await schedule_pregame_collections()
    # 서버 재시작으로 놓친 블로그 잡 catch-up — 홀드 (사용자 요청, 2026-08-14, 재개 지시 전까지 주석 유지)
    # asyncio.create_task(_catchup_blog_posts())
    # Dashboard 동기화 — 서버 기동 시 오늘 아직 안 돌았으면 즉시 1회 실행
    asyncio.create_task(_catchup_dashboard_sync())
    yield
    # Shutdown
    scheduler.shutdown()
    logger.info("Scheduler shut down")


app = FastAPI(
    title="Parking Congestion API",
    description="야구장 주변 주차 혼잡도 분석 API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # Starlette's default 500 (ServerErrorMiddleware) runs outside CORSMiddleware,
    # so an unhandled exception's response never gets CORS headers and browsers
    # report it as a network-level "Failed to fetch" instead of a readable error.
    # A registered exception_handler runs inside CORSMiddleware, so this fixes that
    # for every route in this app, not just dashboard sync.
    logger.error("Unhandled exception on %s %s: %s", request.method, request.url.path, exc, exc_info=True)
    return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})


app.include_router(annual_leave_router)
app.include_router(dashboard_router)  # App Monetization Dashboard (/api/*)
app.include_router(localcur_router)  # 지역화폐 알리미 앱 - 지역 마스터 데이터 (/api/v1/regions/*)
app.include_router(localcur_admin_router)  # 지역화폐 알리미 앱 - 관리자 (/admin/schedules 등, X-Admin-Key 필요)
app.include_router(kfood_trends_public_router)
app.include_router(kfood_trends_admin_router)


@app.get("/parking-status")
async def get_parking_status(
    location: str = Query(..., description="분석할 위치 (예: 잠실, 고척)"),
):
    """Analyze parking congestion around the given location."""
    if not location.strip():
        raise HTTPException(status_code=400, detail="location parameter is required")

    # Check cache
    cached = cache.get(location)
    if cached is not None:
        logger.info("Cache hit for '%s'", location)
        return cached

    # Fetch and analyze
    try:
        result = await analyze(location)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.error("Analysis failed for '%s': %s", location, e)
        raise HTTPException(
            status_code=502, detail="Failed to fetch data from Naver API"
        )

    cache.set(location, result)
    return result


@app.get("/games")
async def get_game_schedule(
    date_str: str | None = Query(
        None, alias="date", description="조회 날짜 (YYYY-MM-DD, 기본: 오늘)"
    ),
):
    """Get today's KBO game schedule with ticket links."""
    cache_key = f"games:{date_str or 'today'}"
    cached = cache.get(cache_key)
    if cached is not None:
        logger.info("Cache hit for games '%s'", date_str)
        return cached

    target_date = None
    if date_str:
        try:
            target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(
                status_code=400, detail="Invalid date format. Use YYYY-MM-DD"
            )

    try:
        games = await fetch_kbo_schedule(target_date)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))

    result = {
        "date": (target_date or date.today()).isoformat(),
        "total_games": len(games),
        "games": games,
    }
    # 경기 중인 게임이 있으면 짧은 TTL로 캐시 — 그래야 클라이언트의 실시간
    # 폴링(15~20초)이 실제로 새 베이스/아웃/카운트 데이터를 받아온다.
    # (기존엔 기본 5분 캐시라 경기 중에도 화면이 최대 5분간 멈춰 보였음)
    live = any(g.get("status") == "경기 중" for g in games)
    ttl = 15 if live else 300
    cache.set(cache_key, result, ttl=ttl)
    return result


@app.get("/postseason")
async def get_postseason(season: int | None = None):
    cache_key = f"postseason_v3:{season or date.today().year}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    try:
        result = await fetch_kbo_postseason(season)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch postseason: {exc}")
    cache.set(cache_key, result, ttl=3600)
    return result


@app.get("/kleague/games")
async def get_kleague_schedule(
    date_str: str | None = Query(
        None, alias="date", description="조회 날짜 (YYYY-MM-DD, 기본: 오늘)"
    ),
):
    """K리그1 경기 일정 + 실시간 스코어 + 이벤트."""
    cache_key = f"kleague:{date_str or 'today'}"
    cached = cache.get(cache_key)
    if cached is not None:
        logger.info("Cache hit for kleague '%s'", date_str)
        return cached

    target_date = None
    if date_str:
        try:
            target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(
                status_code=400, detail="Invalid date format. Use YYYY-MM-DD"
            )

    try:
        games = await fetch_kleague_schedule(target_date)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))

    result = {
        "date": (target_date or date.today()).isoformat(),
        "total_games": len(games),
        "games": games,
    }
    live = any(g.get("status") in ("경기 중", "하프타임") for g in games)
    ttl = 70 if live else 600
    cache.set(cache_key, result, ttl=ttl)
    return result


@app.get("/kleague/upcoming")
async def get_kleague_upcoming(
    days: int = Query(7, description="조회할 일수"),
    limit: int = Query(12, description="최대 경기 수"),
):
    """앞으로 N일간 K리그1 다가올 경기(경기 전 상태만) 일괄 조회.

    클라이언트가 날짜별로 순차 호출하던 것을 서버에서 한 번에 처리해
    모바일 네트워크 지연/실패가 누적되는 문제를 없앤다.
    """
    cache_key = f"kleague:upcoming:{days}:{limit}"
    cached = cache.get(cache_key)
    if cached is not None:
        logger.info("Cache hit for kleague upcoming")
        return cached

    games = await fetch_kleague_upcoming(days=days, limit=limit)
    result = {"total_games": len(games), "games": games}
    cache.set(cache_key, result, ttl=600)
    return result


@app.get("/restaurants")
async def get_restaurants(
    stadium: str = Query(
        ..., description="구장 위치 (예: 잠실, 고척, 사직)"
    ),
):
    """Search restaurants near a stadium with review summary."""
    cache_key = f"restaurants:{stadium}"
    cached = cache.get(cache_key)
    if cached is not None:
        logger.info("Cache hit for restaurants '%s'", stadium)
        return cached

    if not stadium.strip():
        raise HTTPException(status_code=400, detail="stadium parameter is required")

    try:
        result = await search_restaurants(stadium)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.error("Restaurant search failed for '%s': %s", stadium, e)
        raise HTTPException(
            status_code=502, detail="Failed to fetch restaurant data"
        )

    cache.set(cache_key, result)
    return result


@app.get("/stadiums")
async def get_stadiums():
    """전국 KBO 야구장 리스트. 오늘 홈 경기 있는 팀을 앞에 표시."""
    cached_games = cache.get("games:today")
    today_games: list[dict] = cached_games.get("games", []) if cached_games else []

    # 구장별 오늘 홈팀 추출
    home_team_by_location: dict[str, str] = {}
    for game in today_games:
        stadium = game.get("stadium", "")
        full_name = TEAM_FULL_NAME.get(game.get("home_team", ""))
        if not full_name:
            continue
        for location in STADIUM_TEAMS:
            if location in stadium:
                home_team_by_location[location] = full_name
                break

    result = []
    for location, teams in STADIUM_TEAMS.items():
        ordered = list(teams)
        today_home = home_team_by_location.get(location)
        if today_home and today_home in ordered and ordered[0] != today_home:
            ordered.remove(today_home)
            ordered.insert(0, today_home)
        result.append({"location": location, "teams": ordered})

    return result


@app.get("/standings")
async def get_standings():
    """KBO 팀 순위표 (오늘 날짜 기준, 1시간 캐시)."""
    cached = cache.get("standings:kbo")
    if cached:
        logger.info("Cache hit for standings")
        return cached
    try:
        standings = await fetch_kbo_standings()
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    result = {"standings": standings}
    cache.set("standings:kbo", result, ttl=3600)
    return result


def _lookup_kbo_winner(date_str: str, away_team: str, home_team: str) -> str | None:
    """이미 캐시된 /games 응답에서 해당 경기 점수를 찾아 승리팀명을 반환 (없으면 None).

    새 네트워크 호출 없이 기존 games 캐시만 조회한다 — Flutter가 홈 화면에서
    이미 그 날짜의 일정을 불러와 games 캐시가 채워져 있는 경우가 대부분이다.
    """
    for key in (f"games:{date_str}", "games:today"):
        cached = cache.get(key)
        if not cached:
            continue
        for g in cached.get("games", []):
            if g.get("away_team") != away_team or g.get("home_team") != home_team:
                continue
            score = g.get("score")
            if not score or ":" not in score:
                return None
            try:
                away_score, home_score = (int(x.strip()) for x in score.split(":"))
            except ValueError:
                return None
            if away_score == home_score:
                return None
            return home_team if home_score > away_score else away_team
    return None


@app.get("/videos")
async def get_videos(
    away_team: str, home_team: str, date: str, status: str = "", competition_type: str = "",
    competition_name: str = "",
):
    """YouTube 경기 관련 영상 검색 (KBO 기본, competition_type=international 이면 SPOTV 채널 검색).

    정상 결과는 1시간 캐시. 결과가 비어있으면(아직 하이라이트 미등록 등)
    30분 후 재검색을 최대 3회까지 허용하고, 그래도 없으면 1시간 캐시로 늘려
    할당량을 아낀다. KBO는 경기 종료 후 승리팀 공식 채널의 H/L 영상도 함께 찾는다.
    """
    is_intl = competition_type == "international"
    # 국제대회는 KBO와 검색 방식이 달라 캐시를 분리한다 (KBO 캐시 키는 그대로 유지)
    prefix = "videos_intl_v2" if is_intl else "videos_v4"  # v4: 공식 채널별 최대 3개 다양화
    cache_key = f"{prefix}:{date}:{away_team}:{home_team}:{status}"
    retry_key = f"{prefix}_retry:{date}:{away_team}:{home_team}:{status}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    winner_team = (
        _lookup_kbo_winner(date, away_team, home_team) if status == "경기 종료" and not is_intl else None
    )
    try:
        if is_intl:
            videos = await search_international_videos(
                away_team, home_team, date, status=status, competition_name=competition_name,
            )
        else:
            videos = await search_kbo_videos(away_team, home_team, date, status=status, winner_team=winner_team)
    except Exception as e:
        logger.error("YouTube search failed: %s", e)
        videos = []

    result = {"videos": videos}
    if videos:
        cache.set(cache_key, result, ttl=3600)  # 1시간 캐시
        cache.delete(retry_key)
    else:
        retry_count = cache.get(retry_key) or 0
        if retry_count < 3:
            cache.set(cache_key, result, ttl=1800)  # 30분 후 재검색
            cache.set(retry_key, retry_count + 1, ttl=6 * 3600)
        else:
            cache.set(cache_key, result, ttl=3600)  # 3회 재검색해도 없으면 그만 두드림
    return result


@app.get("/youtube-search")
async def youtube_search(q: str = Query(..., description="검색어 (예: KT 오원석 응원가)")):
    """응원가 YouTube 검색 — '야쏭' 채널 우선, 관련성 높은 첫 번째 영상 ID 반환 (24시간 캐시)."""
    cache_key = f"yt_cheer_v2:{q}"
    cached = cache.get(cache_key)
    if cached:
        return cached
    try:
        video_id = await search_cheer_video(q)
    except Exception as e:
        logger.error("YouTube cheer search failed for '%s': %s", q, e)
        raise HTTPException(status_code=502, detail="YouTube search failed")
    result = {"videoId": video_id}
    cache.set(cache_key, result, ttl=86400)
    return result


@app.get("/verse/random")
async def get_random_verse(category: str | None = Query(None, description="카테고리 필터 (사랑/믿음/소망/위로/감사/지혜/평안)")):
    """랜덤 성경 구절 반환. 카테고리 파라미터로 필터링 가능."""
    if not _BIBLE_DATA:
        raise HTTPException(status_code=503, detail="Bible data not loaded")

    pool = _BIBLE_DATA
    if category:
        pool = [v for v in _BIBLE_DATA if v.get("category") == category]
        if not pool:
            raise HTTPException(status_code=404, detail=f"카테고리 '{category}'에 해당하는 구절이 없습니다")

    verse = random.choice(pool)
    return {
        "id": verse["id"],
        "reference": verse["reference"],
        "text": verse["text"],
        "category": verse["category"],
    }


@app.get("/lineup")
async def get_lineup(team: str = Query(..., description="팀 약칭 (예: LG, 두산, KIA)")):
    """저장된 가장 최근 경기 종료 라인업 반환."""
    path = Path(__file__).parent / "data" / "lineups" / f"{team}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"'{team}' 팀 라인업 데이터가 없습니다")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error("Failed to read lineup for %s: %s", team, e)
        raise HTTPException(status_code=500, detail="라인업 파일 읽기 실패")


@app.get("/game-insight")
async def get_game_insight(
    home_team: str = Query(...),
    away_team: str = Query(...),
    home_pitcher: str = Query(""),
    home_era: str = Query(""),
    home_record: str = Query(""),
    away_pitcher: str = Query(""),
    away_era: str = Query(""),
    away_record: str = Query(""),
):
    """AI 선발 매치업 관전 포인트 3개 + 요약 배지 (당일 4시간 캐시)."""
    # date.today()는 서버 시스템 타임존(대개 UTC) 기준이라 KST 자정~09시 사이엔
    # 어제 날짜로 계산되어, 어제 다른 선발로 생성된 캐시가 오늘도 그대로 노출되는
    # 버그가 있었다. KST 기준 날짜로 계산하고, 선발투수도 키에 포함해 당일 중
    # 선발이 바뀌어도 캐시가 자동으로 갱신되게 한다.
    today_kst = datetime.now(KST).date().isoformat()
    cache_key = (
        f"game-insight-v6:{away_team}:{home_team}:{today_kst}:"
        f"{away_pitcher or '?'}:{home_pitcher or '?'}"
    )
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    result = await generate_game_insight(
        home_team=home_team, away_team=away_team,
        home_pitcher=home_pitcher, home_era=home_era, home_record=home_record,
        away_pitcher=away_pitcher, away_era=away_era, away_record=away_record,
    )
    cache.set(cache_key, result, ttl=14400)
    return result


@app.post("/admin/rebuild-lineups")
async def admin_rebuild_lineups():
    """누락된 팀 라인업 파일을 최근 경기 데이터로 재생성."""
    await _rebuild_lineups_if_missing()
    lineups_dir = Path(__file__).parent / "data" / "lineups"
    files = {p.stem: p.stat().st_mtime for p in lineups_dir.glob("*.json")}
    return {"rebuilt": True, "teams": list(files.keys())}


@app.get("/gemini-key")
async def get_gemini_key():
    """Flutter 앱에 Gemini API 키 제공."""
    key = os.getenv("GEMINI_API_KEY", "")
    if not key:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY not configured")
    return {"api_key": key}


@app.get("/supabase-config")
async def get_supabase_config():
    """Flutter 앱에 Supabase URL과 anon key 제공."""
    url = os.getenv("SUPABASE_URL", "")
    key = os.getenv("SUPABASE_KEY", "")
    if not url or not key:
        raise HTTPException(status_code=500, detail="Supabase config not configured")
    return {"url": url, "anon_key": key}


@app.get("/recipe-videos")
async def get_recipe_videos(q: str = Query(..., description="레시피 검색어 (예: 김치찌개)")):
    """K-Food 레시피 YouTube 영상 목록 검색 — title/thumbnail/channelTitle 포함 (24시간 캐시)."""
    cache_key = f"recipe_videos_v4:{q}"
    cached = cache.get(cache_key)
    if cached:
        return cached
    try:
        videos = await search_recipe_videos(q)
    except Exception as e:
        logger.error("Recipe YouTube search failed for '%s': %s", q, e)
        raise HTTPException(status_code=502, detail="YouTube search failed")
    result = {"videos": videos}
    cache.set(cache_key, result, ttl=86400)  # 24시간 — YouTube API 할당량 절약
    return result


@app.post("/translate-titles")
async def translate_titles_endpoint(payload: dict = Body(...)):
    """K-Food 유튜브 영상 제목을 앱 언어로 번역 (제목 단위 30일 캐시).

    영상 검색 자체는 항상 한국어로 하므로(검색 품질 우선) 결과 영상과 원문 제목은
    모든 언어에서 동일하다 — 여기서는 화면 표시용 번역 텍스트만 따로 붙여준다.
    제목 텍스트+언어 조합으로 캐시하므로 같은 영상이 여러 레시피에 걸쳐 나와도
    Gemini를 다시 호출하지 않는다.
    """
    titles = payload.get("titles")
    locale = str(payload.get("locale", "ko"))
    if not isinstance(titles, list) or not titles:
        return {"translations": []}
    titles = [str(t) for t in titles][:20]

    lang_code = locale.split("_")[0].split("-")[0]
    if lang_code == "ko":
        return {"translations": titles}

    def _key(title: str) -> str:
        digest = hashlib.sha1(title.encode("utf-8")).hexdigest()
        return f"title_translate_v2:{lang_code}:{digest}"

    results: list[str] = [""] * len(titles)
    misses: list[str] = []
    miss_indices: list[int] = []
    for i, t in enumerate(titles):
        cached = cache.get(_key(t))
        if cached is not None:
            results[i] = cached
        else:
            misses.append(t)
            miss_indices.append(i)

    if misses:
        try:
            translated = await gemini_translate_titles(misses, lang_code)
        except Exception as e:
            logger.error("translate_titles failed for locale %s: %s", lang_code, e)
            translated = misses
        for idx, orig, tr in zip(miss_indices, misses, translated):
            results[idx] = tr
            cache.set(_key(orig), tr, ttl=2592000)  # 30일 — 제목은 바뀌지 않음

    return {"translations": results}


@app.get("/bible/videos")
async def get_bible_videos(
    q: str = Query(..., description="완성된 검색어 (예: '창세기 1장 설교')"),
    max: int = Query(6, ge=1, le=12),
):
    """Bible Hub — 설교/찬양/찬송가/QT 공용 YouTube 영상 검색 (24시간 캐시)."""
    cache_key = f"bible_videos_v1:{q}:{max}"
    cached = cache.get(cache_key)
    if cached:
        return cached
    try:
        videos = await search_bible_videos(q, max_results=max)
    except Exception as e:
        logger.error("Bible video search failed for '%s': %s", q, e)
        raise HTTPException(status_code=502, detail="YouTube search failed")
    result = {"videos": videos}
    cache.set(cache_key, result, ttl=86400)  # 24시간 — YouTube API 할당량 절약
    return result


@app.get("/bible/ai/topics")
async def get_bible_ai_topics(q: str = Query(..., description="검색어 (성경 구절 또는 주제)")):
    """Bible Hub — 검색어의 핵심 주제 + 관련 구절 AI 분석 (Gemini, 24시간 캐시)."""
    cache_key = f"bible_topics_v1:{q}"
    cached = cache.get(cache_key)
    if cached:
        return cached
    try:
        result = await analyze_bible_topics(q)
    except Exception as e:
        logger.error("Bible topic analysis failed for '%s': %s", q, e)
        raise HTTPException(status_code=502, detail=f"Gemini error: {e}")
    cache.set(cache_key, result, ttl=86400)
    return result


from services.kfood_ai import router as kfood_ai_router
app.include_router(kfood_ai_router)


@app.post("/recipe-summarize")
async def recipe_summarize(
    title: str = Body(..., embed=True),
    steps: str = Body(..., embed=True),
    locale: str = Body("ko", embed=True),
    recipe_id: str = Body("", embed=True),
):
    """K-Food 레시피 AI 요약 (Gemini 서버 측 호출). locale: ko/en/ja/vi/th. 24시간 캐시."""
    # v3: vi/th가 ko 프롬프트로 잘못 응답하던 버그 수정 — 이전 캐시 무효화
    cache_key = f"recipe_summary_v3:{recipe_id or title}:{locale}"
    cached = cache.get(cache_key)
    if cached:
        return cached
    try:
        result = await gemini_summarize_recipe(title, steps, locale)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.error("Recipe summarize failed for '%s': %s", title, e)
        raise HTTPException(status_code=502, detail=f"Gemini error: {e}")
    response = {"result": result}
    cache.set(cache_key, response, ttl=86400)  # 24-hour cache — recipe content rarely changes
    return response


@app.get("/ingredient-substitute")
async def ingredient_substitute(
    ingredient: str = Query(..., description="재료명 (한국어, 예: 고추장)"),
    locale: str = Query("ko", description="응답 언어 (ko/en/ja/vi/th)"),
):
    """K-Food 재료 대체재 AI 추천 (Gemini 서버 측 호출)."""
    # v2: vi/th가 한국어로 강제 응답하던 버그 수정 — 이전 캐시 무효화
    cache_key = f"ing_sub_v2:{ingredient}:{locale}"
    cached = cache.get(cache_key)
    if cached:
        return cached
    try:
        result = await gemini_ingredient_substitute(ingredient, locale)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.error("Ingredient substitute failed for '%s': %s", ingredient, e)
        raise HTTPException(status_code=502, detail=f"Gemini error: {e}")
    data = {"result": result}
    cache.set(cache_key, data, ttl=86400)
    return data


@app.post("/analyze-image")
async def analyze_image_endpoint(
    file: UploadFile = File(...),
    mime_type: str = Form(default="image/jpeg"),
):
    """이미지를 받아 Gemini AI로 분석 후 JSON 문자열 반환."""
    image_bytes = await file.read()
    try:
        result = await gemini_analyze(image_bytes, mime_type)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.error("Gemini analysis failed: %s", e)
        raise HTTPException(status_code=502, detail=f"Gemini API error: {e}")
    return PlainTextResponse(result)


@app.get("/lck/games")
async def get_lck_schedule(
    date_str: str | None = Query(
        None, alias="date", description="조회 날짜 (YYYY-MM-DD, 기본: 오늘)"
    ),
):
    """LCK 경기 일정 + 실시간 세트 스코어."""
    cache_key = f"lck:{date_str or 'today'}"
    cached = cache.get(cache_key)
    if cached is not None:
        logger.info("Cache hit for lck '%s'", date_str)
        return cached

    target_date = None
    if date_str:
        try:
            target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

    try:
        games = await fetch_lck_schedule(target_date)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

    result = {
        "date": (target_date or date.today()).isoformat(),
        "total_games": len(games),
        "games": games,
    }
    live = any(g.get("status") == "경기 중" for g in games)
    ttl = 70 if live else 600
    cache.set(cache_key, result, ttl=ttl)
    return result


@app.get("/lck/games/nearby")
async def get_lck_nearby(
    date_str: str | None = Query(
        None, alias="date", description="기준 날짜 (YYYY-MM-DD, 기본: 오늘)"
    ),
):
    """경기 없는 날 기준 — 최근 완료 경기 + 다음 예정 경기 반환."""
    cache_key = f"lck:nearby:{date_str or 'today'}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    target_date = None
    if date_str:
        try:
            target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

    try:
        result = await fetch_lck_nearby(target_date or date.today())
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

    cache.set(cache_key, result, ttl=600)
    return result


@app.get("/ewc/games")
async def get_ewc_schedule(
    date_str: str | None = Query(
        None, alias="date", description="조회 날짜 (YYYY-MM-DD, 기본: 오늘)"
    ),
):
    """EWC LoL 경기 일정."""
    cache_key = f"ewc:{date_str or 'today'}"
    cached = cache.get(cache_key)
    if cached is not None:
        logger.info("Cache hit for ewc '%s'", date_str)
        return cached

    target_date = None
    if date_str:
        try:
            target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

    try:
        games = await fetch_ewc_schedule(target_date)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))

    result = {
        "date": (target_date or date.today()).isoformat(),
        "total_games": len(games),
        "games": games,
    }
    live = any(g.get("status") == "경기 중" for g in games)
    ttl = 70 if live else 1800
    cache.set(cache_key, result, ttl=ttl)
    return result


@app.get("/ewc/games/nearby")
async def get_ewc_nearby(
    date_str: str | None = Query(
        None, alias="date", description="기준 날짜 (YYYY-MM-DD, 기본: 오늘)"
    ),
):
    """경기 없는 날 기준 — EWC 최근 완료 경기 + 다음 예정 경기 반환."""
    cache_key = f"ewc:nearby:{date_str or 'today'}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    target_date = None
    if date_str:
        try:
            target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

    try:
        result = await fetch_ewc_nearby(target_date or date.today())
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))

    cache.set(cache_key, result, ttl=1800)
    return result


@app.get("/ewc/standings")
async def get_ewc_standings():
    """Road to EWC 팀별 진행 현황 (탈락팀 포함)."""
    cached = cache.get("ewc:standings")
    if cached is not None:
        return cached
    try:
        result = await fetch_ewc_standings()
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    cache.set("ewc:standings", result, ttl=600)
    return result


@app.get("/lck/teams")
async def get_lck_teams():
    """LCK 10개 팀 정보."""
    return {"teams": LCK_TEAMS}


@app.get("/lck/teams/{team_code}")
async def get_lck_team_detail(team_code: str):
    """팀별 로스터 + 최근 경기 5개 (예: /lck/teams/T1)."""
    cache_key = f"lck:team:{team_code.upper()}"
    cached = cache.get(cache_key)
    if cached is not None:
        logger.info("Cache hit for team detail '%s'", team_code)
        return cached

    try:
        result = await fetch_lck_team_detail(team_code.upper())
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))

    cache.set(cache_key, result, ttl=600)
    return result


@app.get("/lck/standings")
async def get_lck_standings():
    """LCK 팀 순위 (LoL Esports API 기준 최신 record)."""
    cached = cache.get("lck:standings")
    if cached is not None:
        return cached

    import httpx as _httpx
    url = "https://esports-api.lolesports.com/persisted/gw/getSchedule"
    headers = {
        "x-api-key": "0TvQnueqKa5mxJntVWt0w4LpLfEkrV1Ta8rQBb9Z",
        "User-Agent": "Mozilla/5.0",
    }
    params = {"hl": "ko-KR", "leagueId": "98767991310872058"}

    try:
        async with _httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers, params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"standings fetch failed: {e}")

    events = data.get("data", {}).get("schedule", {}).get("events", [])

    # 팀별 최신 record 추출 (completed 경기에서 마지막 등장 기준)
    records: dict[str, dict] = {}
    streaks: dict[str, list[str]] = {}  # 최근 경기 결과 순서

    for ev in events:
        if ev.get("type") != "match" or ev.get("state") != "completed":
            continue
        for team in ev.get("match", {}).get("teams", []):
            code = team.get("code", "")
            rec = team.get("record")
            outcome = team.get("result", {}).get("outcome", "")
            if code and rec:
                records[code] = {
                    "wins": rec.get("wins", 0),
                    "losses": rec.get("losses", 0),
                }
            if code and outcome:
                streaks.setdefault(code, []).append(outcome)

    # 연승/연패 계산
    def calc_streak(outcomes: list[str]) -> dict:
        if not outcomes:
            return {"type": None, "count": 0}
        last = outcomes[-1]
        count = 0
        for o in reversed(outcomes):
            if o == last:
                count += 1
            else:
                break
        return {"type": "win" if last == "win" else "loss", "count": count}

    standings = []
    for code, rec in records.items():
        w, l = rec["wins"], rec["losses"]
        total = w + l
        rate = round(w / total, 3) if total else 0.0
        streak = calc_streak(streaks.get(code, []))
        standings.append({
            "code": code,
            "wins": w,
            "losses": l,
            "win_rate": rate,
            "streak": streak,
        })

    standings.sort(key=lambda x: (-x["wins"], x["losses"]))
    for i, s in enumerate(standings):
        s["rank"] = i + 1

    result = {"standings": standings}
    cache.set("lck:standings", result, ttl=600)
    return result


@app.get("/lck/videos")
async def get_lck_videos(
    home_team: str,
    away_team: str,
    home_code: str,
    away_code: str,
    date: str,
    league: str = "LCK",
):
    """YouTube LCK/EWC 경기 하이라이트 영상 검색 (30분 캐시)."""
    cache_key = f"lck_videos:{date}:{home_code}:{away_code}:{league}"
    cached = cache.get(cache_key)
    if cached:
        return cached
    try:
        if league == "EWC":
            videos = await search_ewc_videos(away_team, home_team, away_code, home_code, date)
        else:
            videos = await search_lck_videos(away_team, home_team, away_code, home_code, date)
    except Exception as e:
        logger.error("YouTube search failed: %s", e)
        raise HTTPException(status_code=502, detail="YouTube search failed")
    result = {"videos": videos}
    cache.set(cache_key, result, ttl=1800)
    return result


@app.get("/lck/replay")
async def get_lck_replay(
    home_code: str,
    away_code: str,
    date: str,
):
    """네이버 LCK 경기 다시보기 VOD 목록 (30분 캐시)."""
    cache_key = f"lck_replay:{date}:{home_code}:{away_code}"
    cached = cache.get(cache_key)
    if cached:
        return cached
    try:
        videos = await fetch_naver_lck_replay(home_code, away_code, date)
    except Exception as e:
        logger.error("Naver replay fetch failed: %s", e)
        raise HTTPException(status_code=502, detail="Naver replay fetch failed")
    result = {"videos": videos}
    cache.set(cache_key, result, ttl=1800)
    return result


@app.get("/lck/lineup")
async def get_lck_lineup(home: str, away: str):
    """두 팀 선발 로스터 반환 (탑/정글/미드/원딜/서폿, 10분 캐시)."""
    cache_key = f"lck_lineup:{home}:{away}"
    cached = cache.get(cache_key)
    if cached:
        return cached
    try:
        result = await fetch_lck_lineup(home, away)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    cache.set(cache_key, result, ttl=600)
    return result


@app.get("/riot/account")
async def riot_get_account(game_name: str, tag_line: str):
    """Riot ID(gameName#tagLine) → PUUID 조회."""
    try:
        result = await get_puuid_by_riot_id(game_name, tag_line)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error("Riot account API error: %s", e)
        raise HTTPException(status_code=502, detail="Riot API 호출 실패")
    return result


@app.get("/riot/summoner")
async def riot_get_summoner(puuid: str):
    """PUUID → 소환사 기본 정보(프로필 아이콘, 레벨) 조회."""
    try:
        result = await get_summoner_by_puuid(puuid)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error("Riot summoner API error: %s", e)
        raise HTTPException(status_code=502, detail="Riot API 호출 실패")
    return result


@app.get("/health")
async def health_check():
    return {"status": "ok"}


@app.get("/ads.txt", response_class=PlainTextResponse)
async def ads_txt():
    pub_id = os.getenv("ADSENSE_PUB_ID", "ca-pub-6848418595819302").replace("ca-", "")
    return f"google.com, {pub_id}, DIRECT, f08c47fec0942fa0\n"


# ──────────────────────────────────────────
# Admin: 야구장 업데이트 관리
# ──────────────────────────────────────────

@app.get("/admin/pending-updates")
async def list_pending_updates(
    stadium: str | None = Query(None, description="구장 필터 (잠실/고척/사직 등)"),
    type: str | None = Query(None, description="타입 필터 (parking/restaurant/food/traffic/notice/tip)"),
    limit: int = Query(50, ge=1, le=200, description="최대 반환 개수"),
):
    """승인 대기 중인 업데이트 목록 조회."""
    updates = await get_pending_updates(stadium=stadium, update_type=type, limit=limit)
    return {"total": len(updates), "updates": updates}


@app.post("/admin/approve-update/{update_id}")
async def approve_stadium_update(update_id: int):
    """업데이트 승인 처리 (status: pending → approved)."""
    result = await approve_update(update_id)
    if not result:
        raise HTTPException(status_code=404, detail=f"Update {update_id} not found or already processed")
    return {"message": "approved", "update": result}


@app.post("/admin/reject-update/{update_id}")
async def reject_stadium_update(update_id: int):
    """업데이트 거절 처리 (status: pending → rejected)."""
    result = await reject_update(update_id)
    if not result:
        raise HTTPException(status_code=404, detail=f"Update {update_id} not found or already processed")
    return {"message": "rejected", "update": result}


@app.post("/admin/trigger-collection")
async def trigger_collection(
    stadiums: list[str] | None = Query(None, description="수집할 구장 목록 (없으면 전체)"),
):
    """야구장 정보 수집을 즉시 수동 실행."""
    result = await run_collection(stadiums=stadiums)
    return {"message": "collection triggered", "result": result}


# ──────────────────────────────────────────
# 구단 공지/이벤트 뉴스
# ──────────────────────────────────────────

@app.get("/team-news")
async def get_team_news(
    team: str | None = Query(None, description="구단 필터 (LG/두산/SSG/키움/KT/KIA/롯데/한화/NC)"),
    type: str | None = Query(None, description="유형 필터 (notice/event)"),
    limit: int = Query(50, ge=1, le=200),
):
    """승인된 구단 공지/이벤트 목록 (앱 노출용)."""
    items = await get_approved_team_news(team=team, news_type=type, limit=limit)
    return {"total": len(items), "items": items}


@app.post("/admin/clear-worldcup-cache")
async def clear_worldcup_cache():
    """월드컵 캐시 강제 초기화 (서버 코드 변경 후 즉시 반영용)."""
    cache.delete_prefix("worldcup:")
    return {"cleared": True, "message": "worldcup:* 캐시 삭제 완료"}


@app.get("/sports-news")
async def get_sports_news(
    home_team: str = Query(..., description="홈팀 이름 (예: LG트윈스, 한화이글스)"),
    away_team: str = Query(..., description="원정팀 이름 (예: KIA타이거즈, 두산베어스)"),
    limit: int = Query(15, ge=1, le=50, description="팀당 최대 기사 수"),
):
    """두 팀의 스포츠 미디어 뉴스를 Google News RSS로 실시간 수집."""
    items = await fetch_match_sports_news(home_team, away_team, limit_per_team=limit)
    return {"total": len(items), "items": items}


# --- 커뮤니티 요약 보류 ---
# @app.get("/community-summary")
# async def get_community_summary():
#     lines = cache.get(_COMMUNITY_SUMMARY_KEY)
#     if lines:
#         return {"lines": lines, "cached": True}
#     asyncio.create_task(refresh_community_summary())
#     return {"lines": [], "cached": False}
# --- 여기까지 ---


# ─── 화첩 AI 엔드포인트 ─────────────────────────────────────────────────────

@app.post("/ai/artwork/title")
async def ai_artwork_title(
    image_url: str = Body(..., embed=True),
    mood: str = Body(..., embed=True),
):
    """작품 제목 3개 추천 (URL)."""
    try:
        result = await generate_artwork_title(image_url, mood)
        return json.loads(result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/ai/artwork/poem")
async def ai_artwork_poem(
    image_url: str = Body(..., embed=True),
    mood: str = Body(..., embed=True),
    title: str = Body(..., embed=True),
):
    """작품 시 생성 (URL)."""
    try:
        result = await generate_artwork_poem(image_url, mood, title)
        return json.loads(result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/ai/artwork/analyze")
async def ai_artwork_analyze(
    image_url: str = Body(..., embed=True),
    title: str = Body(..., embed=True),
    mood: str = Body(..., embed=True),
):
    """작품 감상문 생성 (URL)."""
    try:
        result = await generate_artwork_analysis(image_url, title, mood)
        return json.loads(result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# 파일 직접 업로드 방식 (작품 추가 화면용 — 아직 Firebase URL 없을 때)
@app.post("/ai/artwork/title-file")
async def ai_artwork_title_file(
    file: UploadFile = File(...),
    mood: str = Form(...),
):
    """작품 제목 3개 추천 (파일 업로드)."""
    try:
        image_bytes = await file.read()
        result = await generate_artwork_title_from_bytes(image_bytes, file.content_type or "image/jpeg", mood)
        return json.loads(result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/ai/artwork/poem-file")
async def ai_artwork_poem_file(
    file: UploadFile = File(...),
    mood: str = Form(...),
    title: str = Form(...),
):
    """작품 시 생성 (파일 업로드)."""
    try:
        image_bytes = await file.read()
        result = await generate_artwork_poem_from_bytes(image_bytes, file.content_type or "image/jpeg", mood, title)
        return json.loads(result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/ai/artwork/analyze-file")
async def ai_artwork_analyze_file(
    file: UploadFile = File(...),
    title: str = Form(...),
    mood: str = Form(...),
):
    """작품 감상문 생성 (파일 업로드)."""
    try:
        image_bytes = await file.read()
        result = await generate_artwork_analysis_from_bytes(image_bytes, file.content_type or "image/jpeg", title, mood)
        return json.loads(result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ────────────────────────────────────────────────────────────────────────────


# ─── 스피치플러스 AI 엔드포인트 ───────────────────────────────────────────────

@app.post("/api/speech/feedback")
async def speech_feedback(
    question: str = Body(..., embed=True),
    category: str = Body(..., embed=True),
    transcript: str = Body(..., embed=True),
    durationSeconds: int = Body(..., embed=True),
    language: str = Body("ko", embed=True),
):
    """스피치플러스 — 답변 텍스트(transcript)를 Gemini로 평가."""
    try:
        data = await generate_speech_feedback(question, category, transcript, durationSeconds)
        return {"success": True, "data": data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ────────────────────────────────────────────────────────────────────────────


# ─── 스피치플러스 결제 (AI 코칭권 / 광고 제거) ─────────────────────────────────
# Firebase ID 토큰으로 uid를 검증하고, Google Play 구매를 검증한 뒤 Firestore에
# 잔액/권한을 지급한다. 다른 엔드포인트/앱과 완전히 분리된 네임스페이스.

async def _speech_purchase_uid(authorization: str = Header(...)) -> str:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authorization 헤더가 필요합니다.")
    token = authorization.removeprefix("Bearer ").strip()
    try:
        return await verify_id_token(token)
    except Exception:
        raise HTTPException(status_code=401, detail="인증 토큰이 유효하지 않습니다.")


@app.get("/api/speech/purchase/status")
async def speech_purchase_status(uid: str = Depends(_speech_purchase_uid)):
    try:
        data = await get_purchase_status(uid)
        return {"success": True, "data": data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/speech/purchase/verify")
async def speech_purchase_verify(
    productId: str = Body(..., embed=True),
    purchaseToken: str = Body(..., embed=True),
    uid: str = Depends(_speech_purchase_uid),
):
    try:
        data = await verify_and_grant_purchase(uid, productId, purchaseToken)
        return {"success": True, "data": data}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/speech/purchase/consume")
async def speech_purchase_consume(uid: str = Depends(_speech_purchase_uid)):
    try:
        data = await consume_coaching_credit(uid)
        return {"success": True, "data": data}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ────────────────────────────────────────────────────────────────────────────


@app.get("/admin/team-news")
async def list_pending_team_news(
    team: str | None = Query(None),
    type: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
):
    """승인 대기 중인 구단 공지/이벤트 목록."""
    items = await get_pending_team_news(team=team, news_type=type, limit=limit)
    return {"total": len(items), "items": items}


@app.post("/admin/team-news/{news_id}/approve")
async def approve_team_news_item(news_id: int):
    """구단 공지/이벤트 승인."""
    result = await approve_team_news(news_id)
    if not result:
        raise HTTPException(status_code=404, detail=f"team_news {news_id} not found or already processed")
    return {"message": "approved", "item": result}


@app.post("/admin/team-news/{news_id}/reject")
async def reject_team_news_item(news_id: int):
    """구단 공지/이벤트 거절."""
    result = await reject_team_news(news_id)
    if not result:
        raise HTTPException(status_code=404, detail=f"team_news {news_id} not found or already processed")
    return {"message": "rejected", "item": result}


@app.post("/admin/trigger-team-news")
async def trigger_team_news_collection():
    """구단 공지/이벤트 수집 즉시 실행."""
    asyncio.create_task(collect_team_news())
    return {"message": "team news collection started"}


# ──────────────────────────────────────────
# EPL (English Premier League)
# ──────────────────────────────────────────

_epl_lock = asyncio.Lock()


async def prefetch_epl_schedule():
    """Background job: EPL 경기 일정 pre-fetch."""
    if _epl_lock.locked():
        logger.info("EPL prefetch already running, skipping")
        return
    async with _epl_lock:
        try:
            games = await fetch_epl_schedule()
            result = {
                "date": date.today().isoformat(),
                "total_games": len(games),
                "games": games,
            }
            live = any(g.get("status") in ("경기 중", "하프타임") for g in games)
            interval = 1 if live else 10
            ttl = 70 if live else 600
            cache.set("epl:today", result, ttl=ttl)
            scheduler.reschedule_job("epl_schedule", trigger="interval", minutes=interval)
            logger.info("Pre-fetched EPL schedule: %d games, next poll in %dmin", len(games), interval)
        except Exception as e:
            logger.error("EPL schedule prefetch failed: %s", e)


@app.get("/epl/games")
async def get_epl_schedule(
    date_str: str | None = Query(None, alias="date", description="조회 날짜 (YYYY-MM-DD, 기본: 오늘)"),
):
    """EPL 경기 일정 + 실시간 스코어 + 이벤트."""
    cache_key = f"epl:{date_str or 'today'}"
    cached = cache.get(cache_key)
    if cached is not None:
        logger.info("Cache hit for epl '%s'", date_str)
        return cached

    target_date = None
    if date_str:
        try:
            target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

    try:
        games = await fetch_epl_schedule(target_date)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))

    result = {
        "date": (target_date or date.today()).isoformat(),
        "total_games": len(games),
        "games": games,
    }
    live = any(g.get("status") in ("경기 중", "하프타임") for g in games)
    ttl = 70 if live else 600
    cache.set(cache_key, result, ttl=ttl)
    return result


@app.get("/epl/games/nearby")
async def get_epl_nearby(
    date_str: str | None = Query(None, alias="date", description="기준 날짜 (YYYY-MM-DD, 기본: 오늘)"),
):
    """경기 없는 날 — 최근 완료 + 다음 예정 EPL 경기 반환."""
    cache_key = f"epl:nearby:{date_str or 'today'}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    target_date = None
    if date_str:
        try:
            target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

    try:
        result = await fetch_epl_nearby(target_date or date.today())
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))

    cache.set(cache_key, result, ttl=600)
    return result


@app.get("/epl/team/last-formation")
async def get_team_last_formation(
    team: str = Query(..., description="English team name, e.g. 'Manchester United'"),
):
    """The team's most recent finished match's formation — real data from a
    past match, used client-side only as a fallback label (e.g. "최근 경기
    포메이션") when a scheduled match's own lineup isn't announced yet.
    Never presented as confirmed for the upcoming match."""
    cache_key = f"epl:last_formation:{team}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    result = await fetch_team_last_formation(team)
    # Only cache a real hit — an empty result might just mean the backward
    # search hasn't reached far enough yet, so let the next request retry.
    if result.get("formation"):
        cache.set(cache_key, result, ttl=21600)
    return result


@app.get("/epl/team/upcoming-fixtures")
async def get_team_upcoming_fixtures(
    team: str = Query(..., description="English team name, e.g. 'Arsenal'"),
    limit: int = Query(5, ge=1, le=10),
):
    """[team]'s next [limit] scheduled EPL fixtures — real data, used by the
    club detail screen's "Next Fixtures" section."""
    cache_key = f"epl:upcoming_fixtures:{team}:{limit}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    fixtures = await fetch_team_upcoming_fixtures(team, limit=limit)
    result = {"fixtures": fixtures}
    cache.set(cache_key, result, ttl=1800)
    return result


@app.get("/epl/videos")
async def get_epl_videos(home_team: str, away_team: str, date: str):
    """YouTube EPL match highlight videos (30min cache)."""
    cache_key = f"epl_videos:{date}:{home_team}:{away_team}"
    cached = cache.get(cache_key)
    if cached:
        return cached
    try:
        videos = await search_epl_videos(home_team, away_team, date)
    except Exception as e:
        logger.error("EPL YouTube search failed: %s", e)
        raise HTTPException(status_code=502, detail="YouTube search failed")
    result = {"videos": videos}
    cache.set(cache_key, result, ttl=1800)
    return result


@app.get("/debug/epl/game/{game_id}")
async def debug_epl_game(game_id: str, endpoint: str = "record"):
    """Raw Naver API 응답 확인용 디버그 엔드포인트."""
    import httpx as _httpx
    base = "https://api-gw.sports.naver.com/schedule/games"
    url = f"{base}/{game_id}/{endpoint}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://sports.naver.com/wfootball/schedule/index",
    }
    try:
        async with _httpx.AsyncClient(timeout=10.0, headers=headers) as client:
            resp = await client.get(url)
            return {
                "url": url,
                "status_code": resp.status_code,
                "data": resp.json(),
            }
    except Exception as e:
        return {"url": url, "error": str(e)}


@app.get("/debug/fetch")
async def debug_fetch(url: str, referer: str = "https://sports.naver.com/wfootball/schedule/index"):
    """임의 URL Naver API 응답 확인 (디버그용)."""
    import httpx as _httpx
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": referer,
    }
    try:
        async with _httpx.AsyncClient(timeout=10.0, headers=headers) as client:
            resp = await client.get(url)
            try:
                return {"url": url, "status_code": resp.status_code, "data": resp.json()}
            except Exception:
                return {"url": url, "status_code": resp.status_code, "text": resp.text[:2000]}
    except Exception as e:
        return {"url": url, "error": str(e)}


@app.get("/epl/standings")
async def get_epl_standings():
    """EPL 순위표 (1시간 캐시)."""
    cached = cache.get("epl:standings")
    if cached:
        logger.info("Cache hit for EPL standings")
        return cached
    try:
        standings = await fetch_epl_standings()
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    result = {"standings": standings}
    cache.set("epl:standings", result, ttl=3600)
    return result


@app.get("/epl/games/{game_id}/intelligence")
async def get_epl_match_intelligence(game_id: str, home_team: str, away_team: str):
    """Match Prediction / Key Stats / Form — deterministic, grounded in this
    season's real team stats (no LLM). Omits prediction/stats a team hasn't
    played enough real matches to support, rather than guessing."""
    cache_key = f"epl:intelligence:{home_team}:{away_team}"
    cached = cache.get(cache_key)
    if cached:
        return cached

    try:
        stats = await fetch_season_team_stats()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch season stats: {e}")

    home_stats = stats.get(home_team)
    away_stats = stats.get(away_team)

    result = {
        "prediction": compute_prediction(home_stats, away_stats),
        "key_stats": build_key_stats(home_stats, away_stats),
        "form": build_form(home_stats, away_stats),
        "standings": {
            "home": {"rank": home_stats["rank"], "points": home_stats["points"]} if home_stats else None,
            "away": {"rank": away_stats["rank"], "points": away_stats["points"]} if away_stats else None,
        },
    }
    cache.set(cache_key, result, ttl=3600)
    return result


_ODDS_REFRESH_INTERVAL = timedelta(hours=12)


def _odds_is_fresh(odds_row: dict | None) -> bool:
    if not odds_row or not odds_row.get("updated_at"):
        return False
    try:
        updated_at = datetime.fromisoformat(odds_row["updated_at"].replace("Z", "+00:00"))
    except ValueError:
        return False
    return datetime.now(timezone.utc) - updated_at < _ODDS_REFRESH_INTERVAL


@app.get("/epl/games/{game_id}/market")
async def get_epl_market(
    game_id: str,
    home_team: str,
    away_team: str,
    match_date: str = Query(..., alias="date", description="KST 경기일 (YYYY-MM-DD)"),
):
    """Prediction Engine(기존 Poisson 모델, 실데이터 기반) + API-Football Odds
    -> 시장 예상 확률 -> Model vs Market. Odds/Prediction 모두 Supabase에
    저장되어 화면을 열 때마다 다시 계산/재호출하지 않는다 (Odds는 API-Football
    무료 티어 쿼터 보호를 위해 12시간 안에 갱신된 값이 있으면 재사용).
    Odds가 없으면 market/model_vs_market은 null — 임의 기본값을 만들지 않는다."""
    cache_key = f"epl:market:{game_id}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        target_date = datetime.strptime(match_date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

    # --- Prediction: reuse stored value for this model version, else compute + persist ---
    prediction = await get_prediction(game_id, MODEL_VERSION)
    if not prediction:
        try:
            stats = await fetch_season_team_stats()
        except httpx.HTTPError:
            stats = {}
        computed = compute_prediction(stats.get(home_team), stats.get(away_team))
        if computed:
            prediction = {
                "home_probability": computed["home"],
                "draw_probability": computed["draw"],
                "away_probability": computed["away"],
                "expected_goals_home": computed["expected_goals"]["home"],
                "expected_goals_away": computed["expected_goals"]["away"],
                "home_matches_sample": computed["sample_size"]["home_matches"],
                "away_matches_sample": computed["sample_size"]["away_matches"],
                "model_version": MODEL_VERSION,
            }
            await upsert_prediction({
                "match_id": game_id,
                "home_team": home_team,
                "away_team": away_team,
                "match_date": target_date.isoformat(),
                **prediction,
            })

    # --- Odds -> Market probability: reuse a recent Supabase row, else call API-Football ---
    odds_row = await get_match_odds(game_id)
    if not _odds_is_fresh(odds_row):
        fresh_odds = await fetch_match_odds(home_team, away_team, target_date)
        if fresh_odds:
            odds_row = await upsert_match_odds({
                "match_id": game_id,
                "home_team": home_team,
                "away_team": away_team,
                "match_date": target_date.isoformat(),
                "provider": "api-football",
                **fresh_odds,
            }) or odds_row

    market = None
    if odds_row:
        market = compute_market_probability(
            float(odds_row["home_odds"]),
            float(odds_row["draw_odds"]),
            float(odds_row["away_odds"]),
        )
        market["bookmaker"] = odds_row.get("bookmaker")
        market["updated_at"] = odds_row.get("updated_at")
        # Raw decimal odds alongside the normalized %, so the client can show
        # the bookmaker's actual number rather than only a derived figure.
        market["home_odds"] = float(odds_row["home_odds"])
        market["draw_odds"] = float(odds_row["draw_odds"])
        market["away_odds"] = float(odds_row["away_odds"])

    # --- Model vs Market ---
    model_vs_market = None
    if prediction and market:
        model_vs_market = {
            "home_difference": prediction["home_probability"] - market["home"],
            "draw_difference": prediction["draw_probability"] - market["draw"],
            "away_difference": prediction["away_probability"] - market["away"],
        }
        await upsert_model_vs_market({
            "match_id": game_id,
            "model_version": MODEL_VERSION,
            "odds_provider": "api-football",
            **model_vs_market,
        })

    result = {"prediction": prediction, "market": market, "model_vs_market": model_vs_market}
    cache.set(cache_key, result, ttl=3600)
    return result


class TacticalBattleRequest(BaseModel):
    home_team: str
    away_team: str
    home_formation: str | None = None
    away_formation: str | None = None
    home_top_players: list[dict] = []
    away_top_players: list[dict] = []
    h2h: list[dict] = []


@app.post("/epl/games/{game_id}/tactical-battle")
async def get_epl_tactical_battle(game_id: str, body: TacticalBattleRequest):
    """AI tactical analysis strictly grounded in the real match data the client
    already has (lineup/formation/top players/H2H) plus this season's team
    stats. Cached by match id + a hash of the grounding data, so it only
    regenerates when the underlying real data actually changes."""
    try:
        stats = await fetch_season_team_stats()
    except httpx.HTTPError:
        stats = {}

    ctx = build_match_context(
        home_team=body.home_team,
        away_team=body.away_team,
        home_formation=body.home_formation,
        away_formation=body.away_formation,
        home_top_players=body.home_top_players,
        away_top_players=body.away_top_players,
        home_stats=stats.get(body.home_team),
        away_stats=stats.get(body.away_team),
        h2h=body.h2h,
    )

    signature = _context_signature(ctx)
    cache_key = f"epl:tactical:{game_id}:{signature}:{PROMPT_VERSION}"
    cached = cache.get(cache_key)
    if cached:
        return cached

    result = await generate_tactical_battle(ctx)
    cache.set(cache_key, result, ttl=86400)
    return result


@app.post("/epl/games/{game_id}/tactical-stories")
async def get_epl_tactical_stories(game_id: str, body: TacticalBattleRequest):
    """0-3 short tactical stories (tactical_battle / key_player / space),
    grounded in the same real match data as Tactical Battle. One Gemini call
    per match, cached by match id + grounding-data hash — never regenerated
    just because a user opens the screen again."""
    try:
        stats = await fetch_season_team_stats()
    except httpx.HTTPError:
        stats = {}

    ctx = build_match_context(
        home_team=body.home_team,
        away_team=body.away_team,
        home_formation=body.home_formation,
        away_formation=body.away_formation,
        home_top_players=body.home_top_players,
        away_top_players=body.away_top_players,
        home_stats=stats.get(body.home_team),
        away_stats=stats.get(body.away_team),
        h2h=body.h2h,
    )

    signature = _context_signature(ctx)
    cache_key = f"epl:stories:{game_id}:{signature}:{STORIES_PROMPT_VERSION}"
    cached = cache.get(cache_key)
    if cached:
        return cached

    stories = await generate_tactical_stories(ctx, game_id)
    result = {"matchId": game_id, "stories": stories}
    cache.set(cache_key, result, ttl=86400)
    return result


# ──────────────────────────────────────────
# FIFA 월드컵 2026
# ──────────────────────────────────────────

@app.get("/worldcup/matches")
async def get_worldcup_matches(
    date_str: str | None = Query(None, alias="date", description="조회 날짜 (YYYY-MM-DD, 기본: 오늘)"),
):
    """FIFA 월드컵 2026 경기 일정 + 실시간 스코어 + 이벤트."""
    cache_key = f"worldcup:{date_str or 'today'}"
    cached = cache.get(cache_key)
    if cached is not None:
        logger.info("Cache hit for worldcup '%s'", date_str)
        return cached

    target_date = None
    if date_str:
        try:
            target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

    try:
        games = await fetch_worldcup_schedule(target_date)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))

    result = {
        "date": (target_date or date.today()).isoformat(),
        "total_games": len(games),
        "games": games,
    }
    live = any(g.get("status") in ("경기 중", "하프타임") for g in games)
    ttl = 70 if live else 600
    cache.set(cache_key, result, ttl=ttl)
    return result


@app.get("/worldcup/matches/today")
async def get_worldcup_today():
    """오늘의 FIFA 월드컵 경기 (prefetch 캐시 우선)."""
    cached = cache.get("worldcup:today")
    if cached is not None:
        logger.info("Cache hit for worldcup today")
        return cached

    try:
        games = await fetch_worldcup_schedule()
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))

    result = {
        "date": date.today().isoformat(),
        "total_games": len(games),
        "games": games,
    }
    live = any(g.get("status") in ("경기 중", "하프타임") for g in games)
    ttl = 70 if live else 600
    cache.set("worldcup:today", result, ttl=ttl)
    return result


@app.get("/worldcup/standings")
async def get_worldcup_standings():
    """FIFA 월드컵 2026 조별 순위 (1시간 캐시)."""
    cached = cache.get("worldcup:standings")
    if cached is not None:
        logger.info("Cache hit for worldcup standings")
        return cached
    try:
        standings = await fetch_worldcup_standings()
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    result = {"standings": standings}
    cache.set("worldcup:standings", result, ttl=3600)
    return result


@app.get("/worldcup/bracket")
async def get_worldcup_bracket():
    """FIFA 월드컵 2026 토너먼트 브래킷 (32강~결승, 1시간 캐시)."""
    cached = cache.get("worldcup:bracket")
    if cached is not None:
        logger.info("Cache hit for worldcup bracket")
        return cached
    try:
        bracket = await fetch_worldcup_bracket()
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    result = {"bracket": bracket}
    cache.set("worldcup:bracket", result, ttl=3600)
    return result


@app.get("/worldcup/stadiums")
async def get_worldcup_stadiums():
    """FIFA 월드컵 2026 개최 경기장 목록 (정적, 24시간 캐시)."""
    cached = cache.get("worldcup:stadiums")
    if cached is not None:
        return cached
    result = {"stadiums": WORLDCUP_STADIUMS}
    cache.set("worldcup:stadiums", result, ttl=86400)
    return result


@app.get("/worldcup/match/{game_id}/insight")
async def get_worldcup_match_insight(
    game_id: str,
    home_team: str = Query(..., description="홈팀 이름"),
    away_team: str = Query(..., description="원정팀 이름"),
    group: str = Query("", description="조 (예: A, B)"),
    round_name: str = Query("", description="라운드 (예: 조별리그, 32강)"),
):
    """FIFA 월드컵 경기 AI 관전 포인트 3개 (4시간 캐시)."""
    cache_key = f"worldcup:insight:{home_team}:{away_team}:{date.today().isoformat()}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    points = await generate_worldcup_match_insight(home_team, away_team, group, round_name)
    result = {"game_id": game_id, "home_team": home_team, "away_team": away_team, "points": points}
    cache.set(cache_key, result, ttl=14400)
    return result


@app.get("/worldcup/match/{game_id}/prediction")
async def get_worldcup_win_prediction(
    game_id: str,
    home_team: str = Query(..., description="홈팀 이름"),
    away_team: str = Query(..., description="원정팀 이름"),
):
    """FIFA 월드컵 경기 AI 승부 예측 % (24시간 캐시)."""
    cache_key = f"worldcup:prediction:{home_team}:{away_team}:{date.today().isoformat()}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    prediction = await generate_worldcup_win_prediction(home_team, away_team)
    prediction["game_id"] = game_id
    cache.set(cache_key, prediction, ttl=86400)
    return prediction


@app.get("/worldcup/match/{game_id}/videos")
async def get_worldcup_match_videos(game_id: str, home_team: str = "", away_team: str = ""):
    """월드컵 경기 하이라이트/다시보기 영상 목록 (Chzzk 채널 기반, 30분 캐시)."""
    cache_key = f"worldcup:videos:{game_id}"
    cached = cache.get(cache_key)
    if cached:
        return cached

    _GAME_URL = "https://api-gw.sports.naver.com/schedule/games/{game_id}"
    _CHZZK_VIDEOS_URL = "https://api.chzzk.naver.com/service/v1/channels/{channel_id}/videos"
    _CHZZK_WATCH_URL = "https://chzzk.naver.com/video/{video_no}"
    _HEADERS_NAVER = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://m.sports.naver.com/",
    }
    _HEADERS_CHZZK = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://chzzk.naver.com/",
    }

    async with httpx.AsyncClient(timeout=10) as client:
        # Step 1: 게임 상세에서 chzzkLives 채널 ID 목록 수집
        try:
            resp = await client.get(_GAME_URL.format(game_id=game_id), headers=_HEADERS_NAVER)
            game = resp.json().get("result", {}).get("game", {})
        except Exception:
            game = {}

        # home/away 팀명 확보 (파라미터 우선, 없으면 API 응답)
        h_name = home_team or game.get("homeTeamName", "")
        a_name = away_team or game.get("awayTeamName", "")

        chzzk_lives = game.get("chzzkLives", [])
        seen_channels: set[str] = set()
        channel_ids: list[str] = []
        for live in chzzk_lives:
            cid = live.get("chzzkChannelId", "")
            if cid and cid not in seen_channels:
                seen_channels.add(cid)
                channel_ids.append(cid)

        if not channel_ids:
            result = {"videos": []}
            cache.set(cache_key, result, ttl=300)
            return result

        # Step 2: 각 채널의 최근 영상 수집 후 팀명으로 필터링
        def _format_duration(secs) -> str:
            try:
                s = int(secs)
                h, rem = divmod(s, 3600)
                m, sec = divmod(rem, 60)
                if h:
                    return f"{h}:{m:02d}:{sec:02d}"
                return f"{m}:{sec:02d}"
            except Exception:
                return ""

        all_videos: list[dict] = []
        for channel_id in channel_ids[:2]:  # 최대 2개 채널
            try:
                resp = await client.get(
                    _CHZZK_VIDEOS_URL.format(channel_id=channel_id),
                    headers=_HEADERS_CHZZK,
                    params={"sortType": "LATEST", "videoType": "", "size": "30"},
                )
                videos = resp.json().get("content", {}).get("data", [])
            except Exception:
                continue

            for v in videos:
                title: str = v.get("videoTitle", "")
                # 두 팀명 중 하나라도 제목에 포함되면 포함 (짧은 이름 폴백)
                h_short = h_name[:3] if len(h_name) > 3 else h_name
                a_short = a_name[:3] if len(a_name) > 3 else a_name
                if not (
                    (h_name and h_name in title) or
                    (a_name and a_name in title) or
                    (h_short and h_short in title) or
                    (a_short and a_short in title)
                ):
                    continue
                video_no = v.get("videoNo")
                if not video_no:
                    continue
                thumb = v.get("thumbnailImageUrl", "")
                all_videos.append({
                    "video_no": video_no,
                    "title": title,
                    "thumbnail": thumb,
                    "duration": _format_duration(v.get("duration", 0)),
                    "channel_name": v.get("channel", {}).get("channelName", ""),
                    "url": _CHZZK_WATCH_URL.format(video_no=video_no),
                })

        # 중복 제거 후 video_no 순 역정렬 (최신순)
        seen_nos: set = set()
        deduped = []
        for v in all_videos:
            if v["video_no"] not in seen_nos:
                seen_nos.add(v["video_no"])
                deduped.append(v)
        deduped.sort(key=lambda v: v["video_no"], reverse=True)

        result = {"videos": deduped}
        ttl = 300 if not deduped else 1800
        cache.set(cache_key, result, ttl=ttl)
        return result


# ──────────────────────────────────────────
# Blog Auto-Posting
# ──────────────────────────────────────────

@app.post("/blog/generate-post")
async def blog_generate_post(
    slot: str | None = Body(None, embed=True, description="morning | afternoon | evening (없으면 현재 시간 기준 자동)"),
    wp_status: str = Body("draft", embed=True, description="draft | publish | private"),
    force: bool = Body(False, embed=True, description="하루 최대 발행 수 제한 무시"),
    _: None = Depends(_require_admin),
):
    """영어 가이드 포스트 생성 → WordPress 발행 (EN SEO)."""
    try:
        result = await run_en_guide_pipeline(wp_status=wp_status)
        return result
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/blog/generate-kr-guide")
async def blog_generate_kr_guide(
    slot: str = Body("morning", embed=True, description="morning | afternoon"),
    wp_status: str = Body("publish", embed=True, description="draft | publish | private"),
    _: None = Depends(_require_admin),
):
    """KR 검색형 가이드 포스트 생성 → WordPress 발행 (07시/13시)."""
    try:
        result = await run_kr_guide_pipeline(slot=slot, wp_status=wp_status)
        return result
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/blog/generate-en-guide")
async def blog_generate_en_guide(
    wp_status: str = Body("publish", embed=True, description="draft | publish | private"),
    _: None = Depends(_require_admin),
):
    """EN How-To 가이드 포스트 생성 → WordPress 발행 (evening)."""
    try:
        result = await run_en_guide_pipeline(wp_status=wp_status)
        return result
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.error("Blog pipeline error: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/blog/admin/login")
async def blog_admin_login(
    admin_id: str = Body(..., embed=True),
    admin_pw: str = Body(..., embed=True),
):
    """관리자 로그인 검증."""
    if admin_id != os.getenv("BLOG_ADMIN_ID", "") or admin_pw != os.getenv("BLOG_ADMIN_PW", ""):
        raise HTTPException(status_code=403, detail="관리자 인증 실패")
    return {"ok": True}


@app.get("/blog/admin", response_class=HTMLResponse)
async def blog_admin_page():
    """블로그 포스트 관리자 페이지."""
    return HTMLResponse(content="""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>블로그 관리자</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f5f5f5; color: #333; }
  .header { background: #1a1a2e; color: white; padding: 16px 24px; display: flex; align-items: center; gap: 12px; }
  .header h1 { font-size: 18px; font-weight: 600; }
  .login-box { background: white; border-radius: 12px; padding: 32px; max-width: 360px; margin: 60px auto; box-shadow: 0 2px 12px rgba(0,0,0,0.1); }
  .login-box h2 { font-size: 20px; margin-bottom: 20px; text-align: center; }
  .form-group { margin-bottom: 14px; }
  .form-group label { display: block; font-size: 13px; color: #666; margin-bottom: 4px; }
  .form-group input { width: 100%; padding: 10px 12px; border: 1px solid #ddd; border-radius: 8px; font-size: 14px; }
  .btn { display: inline-block; padding: 10px 18px; border-radius: 8px; border: none; cursor: pointer; font-size: 14px; font-weight: 500; }
  .btn-primary { background: #1a1a2e; color: white; width: 100%; margin-top: 8px; }
  .btn-primary:hover { background: #2d2d4e; }
  .container { max-width: 900px; margin: 24px auto; padding: 0 16px; }
  .toolbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
  .toolbar h2 { font-size: 16px; font-weight: 600; }
  .post-card { background: white; border-radius: 10px; padding: 16px 20px; margin-bottom: 10px; box-shadow: 0 1px 4px rgba(0,0,0,0.08); display: flex; align-items: flex-start; gap: 16px; }
  .post-title { font-size: 15px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .post-title a { color: #1a1a2e; text-decoration: none; }
  .post-title a:hover { text-decoration: underline; }
  #toast { position: fixed; bottom: 24px; right: 24px; background: #333; color: white; padding: 12px 20px; border-radius: 8px; font-size: 14px; display: none; z-index: 200; }
  #toast.show { display: block; }
  .loading { text-align: center; padding: 40px; color: #888; }
  .error-msg { color: #cc0000; font-size: 13px; margin-top: 8px; text-align: center; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .spinner { display: inline-block; width: 14px; height: 14px; border: 2px solid rgba(255,255,255,0.4); border-top-color: #fff; border-radius: 50%; animation: spin 0.7s linear infinite; vertical-align: middle; margin-right: 6px; }
  .btn-loading { opacity: 0.85; cursor: not-allowed !important; }
  .tab-nav { display: flex; gap: 4px; }
  .tab-btn { padding: 7px 16px; border-radius: 8px; border: 2px solid #ddd; cursor: pointer; font-size: 14px; background: white; color: #555; font-weight: 600; }
  #tabTrendlab { border-color: #dc2626; color: #dc2626; }
  #tabTrendlab.active { background: #dc2626; color: white; }
  #tabStats { border-color: #6366f1; color: #6366f1; }
  #tabStats.active { background: #6366f1; color: white; }
  .stat-row { display: flex; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
  .stat-box { flex: 1; min-width: 100px; background: white; border-radius: 10px; padding: 16px; box-shadow: 0 1px 4px rgba(0,0,0,0.08); text-align: center; }
  .stat-num { font-size: 26px; font-weight: 700; color: #1a1a2e; }
  .stat-label { font-size: 12px; color: #888; margin-top: 4px; }
  .chart-card { background: white; border-radius: 10px; padding: 20px; margin-bottom: 16px; box-shadow: 0 1px 4px rgba(0,0,0,0.08); }
  .chart-title { font-size: 14px; font-weight: 600; margin-bottom: 14px; color: #333; }
  .top-post-item { display: flex; align-items: center; gap: 10px; padding: 8px 0; border-bottom: 1px solid #f0f0f0; font-size: 13px; }
  .top-post-item:last-child { border-bottom: none; }
  .top-post-rank { font-size: 11px; font-weight: 700; color: #888; width: 20px; text-align: center; flex-shrink: 0; }
  .top-post-views { font-size: 12px; font-weight: 700; color: #2563eb; flex-shrink: 0; }
  .top-post-title { flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .top-post-title a { color: #1a1a2e; text-decoration: none; }
  .top-post-title a:hover { text-decoration: underline; }
</style>
</head>
<body>
<div class="header">
  <h1>📝 블로그 관리자</h1>
</div>

<!-- 로그인 -->
<div id="loginSection">
  <div class="login-box">
    <h2>관리자 로그인</h2>
    <div class="form-group">
      <label>아이디</label>
      <input type="text" id="adminId" placeholder="관리자 아이디" />
    </div>
    <div class="form-group">
      <label>비밀번호</label>
      <input type="password" id="adminPw" placeholder="비밀번호" onkeydown="if(event.key==='Enter')doLogin()" />
    </div>
    <div id="loginError" class="error-msg"></div>
    <button class="btn btn-primary" onclick="doLogin()">로그인</button>
  </div>
</div>

<!-- 메인 -->
<div id="mainSection" style="display:none">
  <div class="container">
    <div class="toolbar">
      <div class="tab-nav">
        <button class="tab-btn active" id="tabTrendlab" onclick="showTab('trendlab')">🔬 TrendLab</button>
        <button class="tab-btn" id="tabStats" onclick="showTab('stats')">📊 통계</button>
      </div>
      <div style="display:flex;gap:8px">
        <button class="btn" style="background:#eee;color:#333" onclick="doLogout()">로그아웃</button>
      </div>
    </div>

    <!-- TrendLab 탭 -->
    <div id="trendlabTab">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;flex-wrap:wrap;gap:8px;">
        <div>
          <p style="margin:0;font-size:15px;font-weight:700;color:#1e3a5f;">🔬 TrendLab</p>
          <p style="margin:4px 0 0;font-size:12px;color:#6b7280;">네이버 뉴스 + YouTube 수집 → 트렌딩 이슈 TOP10 분석</p>
        </div>
        <button id="trendlab-btn" class="btn" onclick="runTrendLab()" style="font-size:13px;padding:8px 20px;background:#dc2626;color:#fff;border:none;border-radius:8px;cursor:pointer;font-weight:600;">🔬 TrendLab 분석</button>
      </div>
      <div id="trendlab-result"></div>
      <!-- TrendLab 포스트 목록 -->
      <div style="margin-top:20px;border-top:1px solid #e5e7eb;padding-top:16px;">
        <div style="display:flex;gap:6px;margin-bottom:12px;">
          <button id="tlFilterReady" class="btn" style="background:#dc2626;color:#fff;font-size:12px;padding:5px 14px;border-radius:20px;" onclick="setTrendlabFilter('READY')">대기중</button>
          <button id="tlFilterPublished" class="btn" style="background:#e5e7eb;color:#555;font-size:12px;padding:5px 14px;border-radius:20px;" onclick="setTrendlabFilter('PUBLISHED')">발행완료</button>
        </div>
        <div id="trendlabList"><div class="loading">불러오는 중...</div></div>
      </div>
    </div>

    <!-- 통계 탭 -->
    <div id="statsTab" style="display:none">
      <div class="stat-row">
        <div class="stat-box"><div class="stat-num" id="statTotal">-</div><div class="stat-label">전체 포스트</div></div>
        <div class="stat-box"><div class="stat-num" id="statPublished">-</div><div class="stat-label">발행됨</div></div>
        <div class="stat-box"><div class="stat-num" id="statViews">-</div><div class="stat-label">총 조회수</div></div>
      </div>
      <div class="chart-card">
        <div class="chart-title">카테고리별 조회수</div>
        <canvas id="chartTopics"></canvas>
      </div>
      <div class="chart-card">
        <div class="chart-title">조회수 TOP 10</div>
        <div id="topPostsList"><div class="loading">불러오는 중...</div></div>
      </div>
      <div class="chart-card">
        <div class="chart-title">일별 포스팅 수 (최근 30일)</div>
        <canvas id="chartDaily"></canvas>
      </div>
    </div>

  </div>
</div>

<!-- 미리보기 모달 -->
<div id="naverPreviewModal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:1000;align-items:flex-start;justify-content:center;padding:24px;overflow-y:auto;">
  <div style="background:white;border-radius:14px;max-width:720px;width:100%;padding:24px;position:relative;">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;">
      <h3 id="naverPreviewTitle" style="font-size:16px;font-weight:700;margin:0;flex:1;margin-right:12px;"></h3>
      <button onclick="closeNaverPreview()" style="background:#f1f5f9;border:none;border-radius:8px;padding:6px 12px;cursor:pointer;font-size:14px;">✕ 닫기</button>
    </div>
    <div id="naverPreviewBody" style="font-family:'Apple SD Gothic Neo',sans-serif;font-size:15px;line-height:1.9;color:#1f2937;"></div>
    <div style="margin-top:16px;text-align:right;">
      <button onclick="closeNaverPreview()" style="background:#6366f1;color:white;border:none;border-radius:8px;padding:8px 18px;cursor:pointer;font-size:14px;">닫기</button>
    </div>
  </div>
</div>

<div id="toast"></div>

<script>
let creds = null;
let chartDaily = null;
let chartTopics = null;

// 세션 복원 — 새로고침 후에도 로그인 유지 (스크립트 로드 시 즉시 실행)
(function restoreSession() {
  const saved = sessionStorage.getItem('blog_creds');
  if (!saved) return;
  try {
    creds = JSON.parse(saved);
    document.getElementById('loginSection').style.display = 'none';
    document.getElementById('mainSection').style.display = 'block';
    loadTrendlabList();
  } catch(e) { sessionStorage.removeItem('blog_creds'); }
}());

function showTab(tab) {
  document.getElementById('trendlabTab').style.display  = tab === 'trendlab' ? 'block' : 'none';
  document.getElementById('statsTab').style.display     = tab === 'stats'    ? 'block' : 'none';
  document.getElementById('tabTrendlab').classList.toggle('active', tab === 'trendlab');
  document.getElementById('tabStats').classList.toggle('active',    tab === 'stats');
  if (tab === 'stats') loadStats();
  if (tab === 'trendlab') loadTrendlabList();
}

async function runTrendLab() {
  const btn = document.getElementById('trendlab-btn');
  if (btn) { btn.disabled = true; btn.classList.add('btn-loading'); btn.innerHTML = '<span class="spinner"></span>분석 중...'; }
  showToast('🔬 TrendLab 분석 중... (60~90초)');
  try {
    const r = await fetch('/trendlab/analyze', { method: 'POST' });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || '실패');
    const top3 = (d.top10 || []).slice(0, 3).map(t => `${t.rank}. ${t.topic}`).join(' / ');
    showToast((d.top10 || []).length
      ? `✅ Blog TOP${d.top10.length} 분석 완료 | ${top3}`
      : `⚠️ Trend 후보 ${(d.trend_debug||[]).length}개 중 블로그로 쓸 만한 topic 없음 (관련 source 부족)`);
    renderTrendLabResult(d);
  } catch(e) { showToast('TrendLab 실패: ' + e.message); }
  finally { if (btn) { btn.disabled = false; btn.classList.remove('btn-loading'); btn.innerHTML = '🔬 TrendLab 분석'; } }
}

async function generateTrendLabPost(topic, category, btn) {
  if (btn) { btn.disabled = true; btn.classList.add('btn-loading'); btn.innerHTML = '<span class="spinner"></span>생성 중...'; btn.style.opacity = '1'; }
  showToast(`✍️ "${topic}" 포스트 생성 중...`);
  const encTopic = encodeURIComponent(topic);
  const encCat = encodeURIComponent(category);
  try {
    const r = await fetch(`/trendlab/generate?topic=${encTopic}&category=${encCat}`, { method: 'POST' });
    const d = await r.json();
    if (!r.ok) {
      const detail = d.detail;
      const msg = detail && typeof detail === 'object'
        ? `관련 근거 부족으로 생성 중단 (${detail.block_reason || detail.status || '사유 미상'})`
        : (detail || '생성 실패');
      throw new Error(msg);
    }
    showToast(`✅ 생성 완료: ${d.title || ''} (ID: ${d.db_id || '-'})`);
    if (btn) { btn.textContent = '✅ 완료'; btn.style.background = '#16a34a'; }
    loadTrendlabList();
  } catch(e) {
    showToast('생성 실패: ' + e.message);
    if (btn) { btn.disabled = false; btn.classList.remove('btn-loading'); btn.innerHTML = '✍️ 생성'; }
  }
}

const TL_BLOCK_REASON_LABEL = {
  NO_RELEVANT_SOURCES: '관련 source 없음',
  INSUFFICIENT_RELEVANT_SOURCES: '관련 source 부족',
  LOW_SOURCE_RELEVANCE: '핵심 근거 source 부족',
  LOW_AVERAGE_RELEVANCE: '평균 관련성 낮음',
  LOW_TOPIC_PURITY: '주제 순도 낮음',
  DUPLICATE_TOPIC: '유사 topic과 중복',
};

function tlRelTime(iso) {
  if (!iso) return '—';
  const diffMs = Date.now() - new Date(iso).getTime();
  const mins = Math.round(diffMs / 60000);
  if (mins < 1) return '방금';
  if (mins < 60) return `${mins}분 전`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}시간 전`;
  return `${Math.round(hours / 24)}일 전`;
}

function tlScorePill(label, val, color) {
  return `<span style="display:inline-flex;align-items:center;gap:3px;background:${color}14;color:${color};border:1px solid ${color}40;border-radius:6px;padding:2px 8px;font-size:12px;font-weight:700;">${label} ${val ?? '—'}</span>`;
}

function renderTrendLabResult(d) {
  const el = document.getElementById('trendlab-result');
  if (!el) return;
  const catColors = {
    '연예': '#ec4899', '스포츠': '#f97316', '경제': '#10b981',
    '정치': '#6366f1', '게임': '#8b5cf6', 'IT': '#0ea5e9',
    '사회': '#64748b', '교육': '#14b8a6', '기타': '#9ca3af',
  };

  function sourceLine(t) {
    const viewStr = t.total_views >= 10000000 ? `${(t.total_views/10000000).toFixed(1)}천만뷰`
      : t.total_views >= 1000000 ? `${(t.total_views/1000000).toFixed(1)}M뷰`
      : t.total_views >= 1000 ? `${Math.round(t.total_views/1000)}K뷰`
      : (t.total_views > 0 ? `${t.total_views}뷰` : '');
    return `<div style="display:flex;gap:10px;flex-wrap:wrap;font-size:12px;color:#6b7280;">
      <span>📰 전체 <b>${t.total_source_count ?? t.source_diversity ?? 0}</b></span>
      <span>🎯 관련 <b>${t.relevant_source_count ?? 0}</b></span>
      <span>🔗 직접근거 <b>${t.strong_relevant_source_count ?? 0}</b></span>
      <span>🕐 최신 ${tlRelTime(t.latest_relevant_source)}</span>
      ${viewStr ? `<span>👁 ${viewStr}</span>` : ''}
      ${t.reason ? `<span style="color:#7c3aed;font-style:italic;">${t.reason}</span>` : ''}
    </div>`;
  }

  function scoreLine(t) {
    return `<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:6px;">
      ${tlScorePill('🔥 Trend', t.trend_score ?? t.virality, '#dc2626')}
      ${tlScorePill('🎯 Relevance', t.average_relevance, '#0ea5e9')}
      ${tlScorePill('📝 Blog', t.blog_score, '#16a34a')}
      ${tlScorePill('⭐ Final', t.final_score, '#7c3aed')}
    </div>`;
  }

  const rows = (d.top10 || []).map(t => {
    const catColor   = catColors[t.category] || '#9ca3af';
    const todayBadge = t.uploaded_today
      ? '<span style="background:#fef3c7;color:#92400e;font-size:11px;padding:1px 6px;border-radius:4px;">오늘 업로드</span>' : '';
    const novelBadge = t.is_novel
      ? '<span style="background:#dcfce7;color:#166534;font-size:11px;padding:1px 6px;border-radius:4px;">첫 등장</span>' : '';
    const freshBadge = t.freshness_label
      ? `<span style="background:#f1f5f9;color:#334155;font-size:11px;padding:1px 6px;border-radius:4px;">${t.freshness_label}</span>` : '';
    const clusterBadge = (t.cluster && t.cluster !== t.topic)
      ? `<span style="background:#ede9fe;color:#6d28d9;font-size:11px;padding:1px 6px;border-radius:4px;">🔗 ${t.cluster}</span>` : '';
    return `<div style="border:1px solid #e5e7eb;border-radius:10px;padding:12px 16px;margin-bottom:10px;background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.06);">
      <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:6px;margin-bottom:8px;">
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
          <span style="font-size:16px;font-weight:800;color:#dc2626;min-width:22px;">${t.rank}.</span>
          <span style="font-weight:700;font-size:15px;">${t.topic}</span>
          <span style="background:${catColor};color:#fff;font-size:11px;padding:2px 8px;border-radius:10px;">${t.category}</span>
          ${clusterBadge}${freshBadge}${todayBadge}${novelBadge}
        </div>
        <button onclick="generateTrendLabPost('${t.topic.replace(/'/g,"\\'")}','${t.category}',this)"
          style="background:#16a34a;color:#fff;border:none;border-radius:6px;padding:6px 16px;font-size:13px;cursor:pointer;font-weight:600;white-space:nowrap;">
          🟢 생성
        </button>
      </div>
      ${scoreLine(t)}
      ${sourceLine(t)}
    </div>`;
  }).join('');

  const excluded = (d.trend_debug || []).filter(t => !(d.top10 || []).some(b => b.topic === t.topic));
  const excludedRows = excluded.map(t => {
    const label = TL_BLOCK_REASON_LABEL[t.article_block_reason] || (t.is_duplicate ? '유사 topic과 중복' : (t.article_block_reason || '기준 미달'));
    return `<div style="border:1px dashed #e5e7eb;border-radius:10px;padding:10px 14px;margin-bottom:8px;background:#fafafa;opacity:.85;">
      <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:6px;margin-bottom:6px;">
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
          <span style="font-weight:600;font-size:14px;color:#6b7280;">${t.topic}</span>
          <span style="background:#e5e7eb;color:#4b5563;font-size:11px;padding:1px 7px;border-radius:10px;">${t.category}</span>
          <span style="background:#fee2e2;color:#991b1b;font-size:12px;font-weight:700;padding:3px 10px;border-radius:6px;">🔒 자동 제외 · ${label}</span>
        </div>
        <button onclick="generateTrendLabPost('${t.topic.replace(/'/g,"\\'")}','${t.category}',this)"
          title="자동 판정과 무관하게 직접 생성을 시도합니다 (근거 부족 등 사유에 따라 서버에서 다시 차단될 수 있음)"
          style="background:#fff;color:#b45309;border:1px solid #f59e0b;border-radius:6px;padding:5px 14px;font-size:12px;cursor:pointer;font-weight:600;white-space:nowrap;">
          ✍️ 그래도 생성
        </button>
      </div>
      ${scoreLine(t)}
      ${sourceLine(t)}
    </div>`;
  }).join('');

  el.innerHTML = `
    <div style="margin-top:16px;">
      <p style="font-weight:700;margin:0 0 12px;color:#1e3a5f;font-size:15px;">🔥 Blog TOP${(d.top10||[]).length} <span style="font-size:12px;font-weight:400;color:#9ca3af;">수집 ${d.collected_count||0}건 분석 · Trend 후보 ${(d.trend_debug||[]).length}개 중 선별</span></p>
      ${rows || '<div class="loading">오늘은 블로그로 쓸 만큼 관련 source가 확보된 topic이 없습니다.</div>'}
      ${excludedRows ? `<details style="margin-top:8px;">
        <summary style="cursor:pointer;font-size:13px;color:#6b7280;font-weight:600;">🔍 제외된 Trend 후보 ${excluded.length}개 보기 (디버깅용)</summary>
        <div style="margin-top:10px;">${excludedRows}</div>
      </details>` : ''}
    </div>`;
  el.style.display = 'block';
}

async function previewNaverContent(id) {
  try {
    const r = await fetch('/blog/naver/ready/' + id);
    const d = await r.json();
    const modal = document.getElementById('naverPreviewModal');
    document.getElementById('naverPreviewTitle').textContent = d.title || '';
    document.getElementById('naverPreviewBody').innerHTML = (d.content || '').replace(/\\n/g, '');
    modal.style.display = 'flex';
  } catch(e) { showToast('미리보기 실패: ' + e.message); }
}

function closeNaverPreview() {
  document.getElementById('naverPreviewModal').style.display = 'none';
}

let _tlFilter = 'READY';

function setTrendlabFilter(f) {
  _tlFilter = f;
  document.getElementById('tlFilterReady').style.cssText     = f==='READY'     ? 'background:#dc2626;color:#fff;font-size:12px;padding:5px 14px;border-radius:20px;border:none;cursor:pointer;' : 'background:#e5e7eb;color:#555;font-size:12px;padding:5px 14px;border-radius:20px;border:none;cursor:pointer;';
  document.getElementById('tlFilterPublished').style.cssText = f==='PUBLISHED' ? 'background:#dc2626;color:#fff;font-size:12px;padding:5px 14px;border-radius:20px;border:none;cursor:pointer;' : 'background:#e5e7eb;color:#555;font-size:12px;padding:5px 14px;border-radius:20px;border:none;cursor:pointer;';
  loadTrendlabList();
}

function loadTrendlabList() {
  fetch(`/blog/trendlab/posts?limit=100&status=${_tlFilter}`)
    .then(r => r.json())
    .then(posts => {
      const el = document.getElementById('trendlabList');
      if (!el) return;
      if (!posts.length) {
        const msg = _tlFilter === 'PUBLISHED' ? '발행완료된 TrendLab 포스트가 없습니다' : '대기중인 TrendLab 포스트가 없습니다';
        el.innerHTML = `<div class="loading">${msg}</div>`; return;
      }
      el.innerHTML = posts.map(p => {
        const date = (p.created_at || '').slice(0, 16);
        const isPublished = p.status === 'PUBLISHED';
        const catColor = '#dc2626';
        const articleDoc = new DOMParser().parseFromString(p.content || '', 'text/html');
        articleDoc.querySelectorAll('div').forEach(node => {
          if (/3줄 요약|주요 출처/.test(node.textContent)) node.remove();
        });
        const bodyChars = (articleDoc.body.textContent || '').replace(/\\s/g, '').length;
        const lengthNote = bodyChars < 1500 ? ' · 짧은 글 — 자료 보강 권장' : '';
        return `<div class="post-card" id="tl-card-${p.id}" style="border-left:4px solid ${isPublished?'#16a34a':catColor};padding:16px 20px;margin-bottom:12px;border-radius:8px;background:${isPublished?'#f0fdf4':'#fff'};box-shadow:0 1px 4px rgba(0,0,0,.08);">
          <div style="display:flex;gap:14px;align-items:flex-start;">
            ${p.thumbnail_url ? `<img src="${escHtml(p.thumbnail_url)}" onclick="window.open('${escHtml(p.thumbnail_url)}','_blank')" style="width:72px;height:72px;object-fit:cover;border-radius:6px;flex-shrink:0;cursor:pointer;">` : ''}
            <div style="flex:1;min-width:0;">
              <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-bottom:6px;">
                <span style="background:#dc2626;color:#fff;font-size:11px;padding:1px 7px;border-radius:10px;">${p.topic||'기타'}</span>
                <span style="background:${isPublished?'#16a34a':'#2563eb'};color:#fff;font-size:11px;padding:1px 7px;border-radius:10px;">${isPublished?'발행완료':'대기'}</span>
                <span style="font-size:12px;color:#9ca3af;">생성 ${date}</span>
              </div>
              <p class="post-title" style="margin:0 0 8px;font-size:14px;font-weight:600;line-height:1.5;">${escHtml(p.title||'')}</p>
              <p style="margin:0 0 8px;font-size:12px;color:${bodyChars < 1500 ? '#b45309' : '#64748b'};">본문 ${bodyChars.toLocaleString()}자 (요약·공백 제외)${lengthNote}</p>
              <div style="display:flex;gap:6px;flex-wrap:wrap;">
                <button onclick="previewNaverContent(${p.id})" style="background:#6366f1;color:#fff;border:none;border-radius:6px;padding:4px 12px;font-size:12px;cursor:pointer;">미리보기</button>
                ${!isPublished ? `<button onclick="markTrendlabPublished(${p.id})" style="background:#dc2626;color:#fff;border:none;border-radius:6px;padding:4px 12px;font-size:12px;cursor:pointer;">발행완료</button>` : ''}
              </div>
            </div>
          </div>
        </div>`;
      }).join('');
    })
    .catch(() => { const el=document.getElementById('trendlabList'); if(el) el.innerHTML='<div class="loading">불러오기 실패</div>'; });
}

async function markTrendlabPublished(id) {
  if (!confirm('발행 완료로 표시할까요?')) return;
  try {
    const r = await fetch('/blog/trendlab/posts/' + id + '/publish', { method: 'POST' });
    if (!r.ok) throw new Error('실패');
    document.getElementById('tl-card-' + id)?.remove();
    showToast('발행 완료 표시됨');
  } catch(e) { showToast('처리 실패: ' + e.message); }
}

function loadStats() {
  fetch('/blog/stats')
    .then(r => r.json())
    .then(d => {
      document.getElementById('statTotal').textContent = d.total;
      document.getElementById('statPublished').textContent = d.published;
      document.getElementById('statViews').textContent = d.total_views;

      // 일별 차트
      const dlabels = d.daily.map(x => x.date.slice(5));
      const ddata   = d.daily.map(x => x.count);
      if (chartDaily) chartDaily.destroy();
      chartDaily = new Chart(document.getElementById('chartDaily'), {
        type: 'bar',
        data: {
          labels: dlabels,
          datasets: [{ label: '포스트 수', data: ddata, backgroundColor: '#3b82f6', borderRadius: 4 }]
        },
        options: { responsive: true, plugins: { legend: { display: false } }, scales: { y: { beginAtZero: true, ticks: { stepSize: 1 } } } }
      });

      // 카테고리별 차트
      const tlabels = d.by_topic.map(x => x.topic);
      const tdata   = d.by_topic.map(x => x.views);
      const colors  = ['#3b82f6','#10b981','#f59e0b','#ef4444','#8b5cf6','#06b6d4','#ec4899','#84cc16'];
      if (chartTopics) chartTopics.destroy();
      chartTopics = new Chart(document.getElementById('chartTopics'), {
        type: 'bar',
        data: {
          labels: tlabels,
          datasets: [{ label: '조회수', data: tdata, backgroundColor: colors.slice(0, tlabels.length), borderRadius: 4 }]
        },
        options: { indexAxis: 'y', responsive: true, plugins: { legend: { display: false } }, scales: { x: { beginAtZero: true } } }
      });

      // TOP 10 목록
      const listEl = document.getElementById('topPostsList');
      if (!d.top_posts.length) { listEl.innerHTML = '<div class="loading">조회 데이터 없음</div>'; return; }
      listEl.innerHTML = d.top_posts.map((p, i) =>
        `<div class="top-post-item">
          <span class="top-post-rank">${i+1}</span>
          <span class="top-post-views">👁 ${p.views}</span>
          <span class="top-post-title"><a href="${p.url || '#'}" target="_blank">${escHtml(p.title || '')}</a></span>
          <span style="font-size:11px;color:#aaa;flex-shrink:0">${p.date || ''}</span>
        </div>`
      ).join('');
    })
    .catch(() => showToast('통계 로드 실패'));
}

async function doLogin() {
  const id = document.getElementById('adminId').value.trim();
  const pw = document.getElementById('adminPw').value.trim();
  const errEl = document.getElementById('loginError');
  errEl.textContent = '';
  if (!id || !pw) { errEl.textContent = '아이디와 비밀번호를 입력하세요.'; return; }
  const btn = document.querySelector('#loginSection .btn-primary');
  btn.textContent = '로그인 중...';
  btn.disabled = true;
  try {
    const r = await fetch('/blog/admin/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ admin_id: id, admin_pw: pw })
    });
    if (r.status === 403) { errEl.textContent = '아이디 또는 비밀번호가 틀렸습니다.'; return; }
    if (!r.ok) { errEl.textContent = '서버 오류 (' + r.status + '). 다시 시도하세요.'; return; }
    creds = { admin_id: id, admin_pw: pw };
    sessionStorage.setItem('blog_creds', JSON.stringify(creds));
    document.getElementById('loginSection').style.display = 'none';
    document.getElementById('mainSection').style.display = 'block';
    loadTrendlabList();
  } catch(e) {
    errEl.textContent = '연결 실패: ' + e.message;
  } finally {
    btn.textContent = '로그인';
    btn.disabled = false;
  }
}

function doLogout() {
  creds = null;
  sessionStorage.removeItem('blog_creds');
  document.getElementById('loginSection').style.display = 'block';
  document.getElementById('mainSection').style.display = 'none';
  document.getElementById('adminId').value = '';
  document.getElementById('adminPw').value = '';
  document.getElementById('loginError').textContent = '';
}

function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 3000);
}
function escHtml(s) { return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
</script>
</body>
</html>""")


@app.get("/blog/stats")
async def blog_stats_api():
    """블로그 통계 데이터 (관리자 대시보드용)."""
    from services.blog_db import get_stats
    return get_stats()


# ── Naver READY 엔드포인트 ────────────────────────────────────────────────────

@app.get("/blog/naver/ready")
async def naver_ready_list(limit: int = Query(50, ge=1, le=200), status: str = Query("READY")):
    """네이버 포스트 목록 (trendlab 제외). status=READY|PUBLISHED|ALL"""
    from storage.post_repository import PostRepository
    repo = PostRepository()
    st = None if status.upper() == "ALL" else status.upper()
    posts = repo.get_posts_filtered(platform="naver", exclude_source="trendlab", status=st, limit=limit)
    return posts


@app.get("/blog/trendlab/posts")
async def trendlab_posts_list(limit: int = Query(50, ge=1, le=200), status: str = Query("READY")):
    """TrendLab 생성 포스트 목록. status=READY|PUBLISHED|ALL"""
    from storage.post_repository import PostRepository
    repo = PostRepository()
    st = None if status.upper() == "ALL" else status.upper()
    posts = repo.get_posts_filtered(source="trendlab", status=st, limit=limit)
    return posts


@app.post("/blog/trendlab/posts/{post_id}/publish")
async def trendlab_mark_published(post_id: int):
    """TrendLab 포스트를 PUBLISHED로 표시."""
    from storage.post_repository import PostRepository, Status
    from datetime import datetime as _dt
    repo = PostRepository()
    with repo._conn() as conn:
        conn.execute(
            "UPDATE blog_posts SET status=?, published_at=? WHERE id=? AND source='trendlab'",
            (Status.PUBLISHED, _dt.now().isoformat(), post_id),
        )
        conn.commit()
    return {"ok": True, "post_id": post_id, "status": "PUBLISHED"}


@app.delete("/blog/naver/ready/{post_id}")
async def naver_discard_post(post_id: int):
    """네이버 READY 포스트 폐기 (완전 삭제)."""
    from storage.post_repository import PostRepository
    repo = PostRepository()
    with repo._conn() as conn:
        cur = conn.execute(
            "DELETE FROM blog_posts WHERE id=? AND platform='naver'", (post_id,)
        )
        conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="포스트를 찾을 수 없습니다")
    return {"ok": True, "post_id": post_id, "status": "DISCARDED"}


@app.get("/blog/naver/ready/{post_id}")
async def naver_ready_detail(post_id: int):
    """네이버 READY 포스트 상세 (본문 포함)."""
    from storage.post_repository import PostRepository
    import sqlite3
    repo = PostRepository()
    with repo._conn() as conn:
        row = conn.execute(
            "SELECT * FROM blog_posts WHERE id=? AND platform='naver'", (post_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="포스트를 찾을 수 없습니다")
    return dict(row)


@app.post("/blog/naver/ready/{post_id}/publish")
async def naver_mark_published(post_id: int):
    """네이버 포스트를 PUBLISHED로 표시 (수동 발행 완료 후)."""
    from storage.post_repository import PostRepository, Status
    from datetime import datetime as _dt
    repo = PostRepository()
    with repo._conn() as conn:
        conn.execute(
            "UPDATE blog_posts SET status=?, published_at=? WHERE id=? AND platform='naver'",
            (Status.PUBLISHED, _dt.now().isoformat(), post_id),
        )
        conn.commit()
    return {"ok": True, "post_id": post_id, "status": "PUBLISHED"}


@app.post("/blog/naver/generate")
async def naver_generate_now(keyword: str | None = None, category: str | None = None):
    """즉시 네이버 READY 콘텐츠 생성. keyword 지정 시 해당 주제로 강제 생성."""
    try:
        result = await run_naver_ready_pipeline(
            slot="manual",
            forced_keyword=keyword or None,
            forced_category=category or None,
        )
        return result
    except Exception as e:
        logger.error("Naver generate failed: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/trendlab/analyze")
async def trendlab_analyze():
    """TrendLab: 네이버+YouTube 수집 → TOP10 이슈구문 분석. 포스트 생성 없음."""
    try:
        from app.trendlab.service import run_trendlab_analyze
        result = await run_trendlab_analyze()
        return result
    except Exception as e:
        logger.error("TrendLab analyze failed: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/trendlab/generate")
async def trendlab_generate(topic: str = Query(...), category: str = Query(default="기타")):
    """TrendLab: 특정 토픽으로 포스트 생성 → drafts + DB READY 저장."""
    from app.trendlab.relevance import TrendLabBlockedError
    try:
        from app.trendlab.service import run_trendlab_generate
        result = await run_trendlab_generate(topic, category)
        return result
    except TrendLabBlockedError as e:
        # 서버 오류가 아니라 "이 topic은 관련 근거가 부족해 글을 쓰지 않는다"는 정상적인
        # 시스템 판단 — 422로 구조화된 사유를 그대로 돌려준다.
        logger.info("TrendLab generate blocked: %s", e.detail)
        raise HTTPException(status_code=422, detail=e.detail)
    except Exception as e:
        logger.error("TrendLab generate failed: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/trendlab/drafts")
async def trendlab_drafts():
    """TrendLab 최근 생성 결과 조회."""
    import json as _json
    from pathlib import Path
    draft_path = Path("drafts/trend_test.json")
    if not draft_path.exists():
        return {"ok": False, "message": "아직 생성된 결과 없음"}
    data = _json.loads(draft_path.read_text(encoding="utf-8"))
    return {"ok": True, "data": data}


@app.get("/blog/posts")
async def blog_list_posts(limit: int = Query(20, ge=1, le=100)):
    """Kekegroup(WordPress) 포스트 로그 조회."""
    from storage.post_repository import PostRepository
    repo = PostRepository()
    posts = repo.get_posts_filtered(platform="wordpress", limit=limit)
    return {"total": len(posts), "posts": posts}


@app.delete("/blog/posts/{post_id}")
async def blog_delete_post(post_id: int, _: None = Depends(_require_admin)):
    """WordPress 포스트 삭제."""
    try:
        result = await wp_delete_post(post_id)
        return result
    except Exception as e:
        logger.error("Blog delete error: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/blog/posts/{post_id}/view")
async def blog_view_post(post_id: int):
    """조회수 +1 후 WordPress 포스트로 리다이렉트."""
    from services.blog_db import increment_view_count, get_recent_posts
    increment_view_count(post_id)
    posts = get_recent_posts(limit=100)
    url = next((p["wp_url"] for p in posts if p.get("wp_post_id") == post_id), None)
    if not url:
        wp_base = os.getenv("WORDPRESS_URL", "https://blog.kekegroup.uk")
        url = f"{wp_base}/?p={post_id}"
    return RedirectResponse(url=url, status_code=302)


_TRACKING_PIXEL = (
    b"\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff"
    b"\x00\x00\x00\x21\xf9\x04\x00\x00\x00\x00\x00\x2c\x00\x00\x00\x00"
    b"\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b"
)


@app.get("/blog/track/{post_id}")
async def blog_track_post(post_id: int):
    """블로그 방문자 조회수 트래킹 픽셀."""
    from services.blog_db import increment_view_count
    increment_view_count(post_id)
    return Response(
        content=_TRACKING_PIXEL,
        media_type="image/gif",
        headers={"Cache-Control": "no-store, no-cache"},
    )


@app.delete("/blog/posts/{post_id}/image")
async def blog_remove_post_image(post_id: int, _: None = Depends(_require_admin)):
    """WordPress 포스트 대표 이미지(썸네일) 제거."""
    try:
        result = await wp_edit_post(post_id, {"post_thumbnail": 0})
        return {"post_id": post_id, "image_removed": True, "url": result.get("url", "")}
    except Exception as e:
        logger.error("Blog remove image error: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


@app.patch("/blog/posts/{post_id}")
async def blog_edit_post(
    post_id: int,
    status: str | None = Body(None, embed=True, description="publish | draft | trash"),
    title: str | None = Body(None, embed=True),
    _: None = Depends(_require_admin),
):
    """WordPress 포스트 상태/제목 수정."""
    fields = {}
    if status:
        fields["post_status"] = status
    if title:
        fields["post_title"] = title
    if not fields:
        raise HTTPException(status_code=400, detail="수정할 항목(status, title)을 입력하세요")
    try:
        result = await wp_edit_post(post_id, fields)
        return result
    except Exception as e:
        logger.error("Blog edit error: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/blog/admin/setup-branding")
async def blog_setup_branding(_: None = Depends(_require_admin)):
    """WordPress 사이트명·태그라인·카테고리를 HustleScope 브랜딩으로 일괄 변경."""
    result = await wp_setup_branding(
        title="HustleScope",
        tagline="AI · Automation · Markets — Read the market, not just the news.",
        category_map={
            "주식·코인":  "Markets",
            "AI·테크":   "AI",
            "경제·정책":  "Economy",
            "게임":       "Game",
            "Market":    "Markets",
            "Automation": "Automation",
            "Guides":    "Guides",
            "Crypto":    "Crypto",
        },
    )
    return result


@app.post("/blog/admin/setup-menu")
async def blog_setup_menu(_: None = Depends(_require_admin)):
    """WordPress 네비게이션 메뉴를 영어로 교체 (plan.md 메뉴 구조 반영)."""
    wp_base = os.getenv("WORDPRESS_URL", "https://blog.kekegroup.uk").rstrip("/")
    menu_items = [
        {"title": "Home",       "url": f"{wp_base}/"},
        {"title": "AI",         "url": f"{wp_base}/category/ai-tech/"},
        {"title": "Automation", "url": f"{wp_base}/category/automation/"},
        {"title": "Markets",    "url": f"{wp_base}/category/markets/"},
        {"title": "Guides",     "url": f"{wp_base}/category/guides/"},
    ]
    result = await wp_setup_nav_menu(menu_items)
    return result


@app.post("/blog/trader-post")
async def blog_trader_post(
    wp_status: str = Body("publish", embed=True, description="publish | draft"),
):
    """doc/trader.txt 기반 한국 주식 포스팅 즉시 실행."""
    try:
        result = await run_trader_pipeline(wp_status=wp_status)
        return result
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("Trader pipeline error: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/blog/keywords")
async def blog_list_keywords(limit: int = Query(100, ge=1, le=500)):
    """사용된 키워드 목록 조회."""
    keywords = get_used_keywords(limit=limit)
    return {"total": len(keywords), "keywords": keywords}


# ── KekeGroup 블로그 테마 CSS ────────────────────────────────────────────────

_KEKE_BLOG_CSS = """
/* === KekeGroup Blog Theme === */

/* ─ 색상 변수 ─ */
:root {
    --keke-primary: #2563eb;
    --keke-text: #111827;
    --keke-border: #e5e7eb;
    --keke-bg: #ffffff;
}
a { color: var(--keke-primary); }
a:hover { color: #1d4ed8; }

/* ─ 3줄 요약 박스 ─ */
.keke-summary-box {
    background: #f0f9ff !important;
    border-left: 4px solid #2563eb !important;
    padding: 16px 20px !important;
    margin: 0 0 28px !important;
    border-radius: 0 8px 8px 0 !important;
}
.keke-summary-box ul {
    margin: 8px 0 0 !important;
    padding: 0 !important;
    list-style: none !important;
}
.keke-summary-box li {
    padding: 3px 0 !important;
    color: #1f2937 !important;
    font-size: 15px !important;
    line-height: 1.6 !important;
    border: none !important;
}

/* ─ 포스트 목록 카드 ─ */
article.post,
.post.type-post,
.entry {
    border: 1px solid var(--keke-border) !important;
    border-radius: 8px !important;
    padding: 16px !important;
    margin-bottom: 20px !important;
    background: var(--keke-bg) !important;
    box-shadow: 0 1px 3px rgba(0,0,0,.05) !important;
    transition: box-shadow .2s !important;
}
article.post:hover,
.post.type-post:hover {
    box-shadow: 0 4px 12px rgba(37,99,235,.12) !important;
}

/* 썸네일 고정 크기 (아카이브) */
.post-thumbnail img,
.wp-post-image {
    border-radius: 8px !important;
    object-fit: cover !important;
}
.archive .post-thumbnail img,
.home .post-thumbnail img {
    width: 140px !important;
    height: 100px !important;
}

/* ─ 카테고리 배지 ─ */
.cat-links a,
.entry-categories a,
.post-categories a {
    display: inline-block !important;
    padding: 2px 10px !important;
    border-radius: 4px !important;
    font-size: 12px !important;
    font-weight: 600 !important;
    text-decoration: none !important;
    color: #fff !important;
    background: var(--keke-primary) !important;
    margin-right: 4px !important;
}
/* AI·테크 */
.cat-links a[href*="ai"],
.cat-links a[href*="ai-tech"],
.cat-links a[href*="%EC%95%84%EC%9D%B4"],
.category-ai-teuku .cat-links a { background: #7c3aed !important; }
/* 주식·코인 */
.cat-links a[href*="stock"],
.cat-links a[href*="%EC%A3%BC%EC%8B%9D"],
.cat-links a[href*="%EC%BD%94%EC%9D%B8"],
.category-jugsig .cat-links a { background: #059669 !important; }
/* 코인 단독 */
.cat-links a[href*="coin"] { background: #d97706 !important; }
/* 경제·정책 */
.cat-links a[href*="policy"],
.cat-links a[href*="%EC%A0%95%EC%B1%85"],
.cat-links a[href*="%EA%B2%BD%EC%A0%9C"],
.category-policy .cat-links a { background: #2563eb !important; }

/* ─ 글 상세 대표 이미지 ─ */
.single .wp-post-image,
.single .post-thumbnail img {
    width: 100% !important;
    max-height: 420px !important;
    object-fit: cover !important;
    border-radius: 8px !important;
    margin-bottom: 24px !important;
    display: block !important;
}

/* ─ 인기글 위젯 번호 배지 ─ */
.widget_popular_posts ol,
.widget_top-posts ol,
.popular-posts ol {
    list-style: none !important;
    padding: 0 !important;
    counter-reset: popular-ctr !important;
}
.widget_popular_posts ol li,
.widget_top-posts ol li,
.popular-posts ol li {
    padding: 9px 0 9px 36px !important;
    border-bottom: 1px solid var(--keke-border) !important;
    position: relative !important;
    counter-increment: popular-ctr !important;
    font-size: 14px !important;
    line-height: 1.5 !important;
}
.widget_popular_posts ol li::before,
.widget_top-posts ol li::before,
.popular-posts ol li::before {
    content: counter(popular-ctr) !important;
    position: absolute !important;
    left: 0 !important;
    top: 50% !important;
    transform: translateY(-50%) !important;
    width: 24px !important;
    height: 24px !important;
    background: var(--keke-primary) !important;
    color: #fff !important;
    border-radius: 50% !important;
    font-size: 11px !important;
    font-weight: 700 !important;
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
    line-height: 1 !important;
}
.widget_popular_posts ol li:nth-child(1)::before,
.widget_top-posts ol li:nth-child(1)::before { background: #ef4444 !important; }
.widget_popular_posts ol li:nth-child(2)::before,
.widget_top-posts ol li:nth-child(2)::before { background: #f97316 !important; }
.widget_popular_posts ol li:nth-child(3)::before,
.widget_top-posts ol li:nth-child(3)::before { background: #eab308 !important; }

/* ─ 헤더 포인트 컬러 ─ */
.site-header,
#site-header,
header.site-header {
    border-bottom: 2px solid var(--keke-primary) !important;
}
.site-title a,
.site-title a:hover { color: var(--keke-primary) !important; }

/* ─ 광고 여백 ─ */
.keke-ad-top, .keke-ad-mid, .keke-ad-bottom {
    margin: 28px 0 !important;
    text-align: center !important;
}

/* ─ 본문 가독성 ─ */
.entry-content p,
.post-content p {
    color: var(--keke-text) !important;
    line-height: 1.85 !important;
    font-size: 16px !important;
}
.entry-content h2,
.post-content h2 {
    color: #1e3a8a !important;
    border-bottom: 2px solid #dbeafe !important;
    padding-bottom: 6px !important;
    margin-top: 36px !important;
}
.entry-content h3,
.post-content h3 {
    color: #1e40af !important;
    margin-top: 24px !important;
}

/* ─ 태그 클라우드 ─ */
.tagcloud a,
.tag-cloud-link {
    display: inline-block !important;
    padding: 4px 10px !important;
    border: 1px solid var(--keke-border) !important;
    border-radius: 20px !important;
    font-size: 13px !important;
    color: var(--keke-text) !important;
    text-decoration: none !important;
    margin: 3px !important;
    transition: all .2s !important;
    background: var(--keke-bg) !important;
}
.tagcloud a:hover,
.tag-cloud-link:hover {
    background: var(--keke-primary) !important;
    color: #fff !important;
    border-color: var(--keke-primary) !important;
}

/* ─ 오늘의 핵심 이슈 (sticky/featured post) ─ */
.sticky article,
article.sticky {
    border: 2px solid var(--keke-primary) !important;
    border-radius: 10px !important;
    background: #eff6ff !important;
}

/* ─ mark 강조 태그 가독성 ─ */
mark {
    background: none !important;
    color: #1d4ed8 !important;
    font-weight: 700 !important;
    border-bottom: 2px solid #93c5fd !important;
    padding: 0 !important;
    border-radius: 0 !important;
}
.conclusion-box mark {
    background: none !important;
    color: #fef08a !important;
    border-bottom: none !important;
}
.conclusion-box h2,
.conclusion-box h3 {
    color: #bfdbfe !important;
    border: none !important;
    border-bottom: none !important;
    padding-left: 0 !important;
    margin-top: 0 !important;
}
.conclusion-box a,
.conclusion-box p a {
    color: #bfdbfe !important;
    border-bottom: 1px solid rgba(191,219,254,0.4) !important;
}

/* ─ 작성자·카테고리 메타 숨김 (Written by / in Category) ─ */
.entry-meta,
.byline,
.posted-by,
.author-info,
.entry-author,
.author.vcard,
span.author,
.single .entry-footer,
.entry-meta .cat-links,
.entry-footer .cat-links,
.entry-footer .tags-links,
.post-author,
.post-meta,
.wp-block-post-author,
[class*="author"],
[class*="byline"],
[class*="posted-by"] {
    display: none !important;
}
"""


@app.post("/blog/apply-theme-css")
async def blog_apply_theme_css():
    """KekeGroup 블로그 테마 CSS를 WordPress Additional CSS에 적용."""
    try:
        result = await update_additional_css(_KEKE_BLOG_CSS)
        return {"message": "테마 CSS 적용 완료", **result}
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.error("Theme CSS apply failed: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/blog/admin/fix-cat-pagination")
async def blog_fix_cat_pagination():
    """카테고리 포스트 위젯 페이지네이션 상태 유지 JS를 WordPress에 주입."""
    try:
        result = await wp_inject_cat_pagination_fix()
        return {"message": "페이지네이션 JS 주입 완료", **result}
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.error("Cat pagination fix failed: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/blog/admin/patch-posts-pagination")
async def blog_patch_posts_pagination():
    """기존 WordPress 포스트 전체에 카테고리 페이지네이션 JS 패치."""
    try:
        result = await wp_patch_all_posts_pagination()
        return {"message": "패치 완료", **result}
    except Exception as e:
        logger.error("Patch posts pagination failed: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/blog/admin/patch-jsonld")
async def blog_patch_jsonld():
    """기존 WordPress 발행 포스트 전체에 JSON-LD image/keywords 필드 일괄 패치."""
    import xmlrpc.client
    from services.blog_wordpress_service import _get_config
    from services.blog_pipeline import _inject_json_ld
    from services.blog_db import get_recent_posts

    try:
        xmlrpc_url, username, password = _get_config()
        server = xmlrpc.client.ServerProxy(xmlrpc_url, allow_none=True)

        db_posts = get_recent_posts(limit=500)
        published = [p for p in db_posts if p.get("wp_post_id") and p.get("wp_url") and p.get("status") == "PUBLISHED"]

        updated, skipped, failed = 0, 0, 0
        for p in published:
            try:
                wp_id = int(p["wp_post_id"])
                try:
                    info = server.wp.getPost(0, username, password, wp_id, ["post_content", "post_status"])
                except Exception:
                    info = None
                if not info or info.get("post_status") != "publish":
                    skipped += 1
                    continue
                existing = info.get("post_content", "") or ""
                # 이미 image 필드가 있는 JSON-LD면 건너뜀
                if '"@type": "ImageObject"' in existing and '"width"' in existing:
                    skipped += 1
                    continue
                # 기존 JSON-LD 블록 제거 후 새 것 삽입
                import re as _re
                existing_clean = _re.sub(
                    r'<script type="application/ld\+json">.*?</script>\s*',
                    "", existing, flags=_re.DOTALL
                )
                lang = "en" if (p.get("topic") or "").endswith("_en") else "ko"
                thumbnail = p.get("thumbnail_url") or ""
                tags_raw = p.get("tags")
                import json as _json
                try:
                    tags_list = _json.loads(tags_raw) if isinstance(tags_raw, str) else []
                    if not isinstance(tags_list, list):
                        tags_list = []
                except Exception:
                    tags_list = []
                new_ld = _inject_json_ld(
                    p.get("title") or "", p.get("meta_description") or "", p.get("wp_url") or "",
                    lang=lang, image_url=thumbnail, tags=tags_list,
                )
                server.wp.editPost(0, username, password, wp_id, {"post_content": new_ld + existing_clean})
                updated += 1
            except Exception as e:
                logger.warning("JSON-LD patch failed for wp_id=%s: %s", p.get("wp_post_id"), e)
                failed += 1

        logger.info("JSON-LD patch done — updated=%d skipped=%d failed=%d", updated, skipped, failed)
        return {"updated": updated, "skipped": skipped, "failed": failed}
    except Exception as e:
        logger.error("JSON-LD patch endpoint failed: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


# ── YouTube AI Copilot ───────────────────────────────────────────────────────

from services.ytcopilot_service import (
    get_usage as ytc_get_usage,
    can_generate as ytc_can_generate,
    increment_usage as ytc_increment_usage,
    generate_titles as ytc_titles,
    generate_description as ytc_description,
    generate_tags as ytc_tags,
    generate_thumbnail_text as ytc_thumbnail,
    generate_script as ytc_script,
    optimize_video as ytc_optimize,
)


@app.get("/ytcopilot/usage/{device_id}")
async def ytcopilot_usage(device_id: str):
    return ytc_get_usage(device_id)


@app.post("/ytcopilot/generate")
async def ytcopilot_generate(body: dict = Body(...)):
    device_id = (body.get("device_id") or "").strip()
    gen_type = (body.get("type") or "").strip()
    topic = (body.get("topic") or "").strip()
    duration = int(body.get("duration") or 60)

    if not device_id or not gen_type or not topic:
        raise HTTPException(400, "device_id, type, topic are required")

    if not ytc_can_generate(device_id):
        raise HTTPException(429, "오늘 무료 생성 횟수(5회)를 모두 사용했습니다.")

    try:
        if gen_type == "titles":
            result = await ytc_titles(topic)
        elif gen_type == "description":
            result = await ytc_description(topic)
        elif gen_type == "tags":
            result = await ytc_tags(topic)
        elif gen_type == "thumbnail":
            result = await ytc_thumbnail(topic)
        elif gen_type == "script":
            result = await ytc_script(topic, duration)
        else:
            raise HTTPException(400, f"Unknown type: {gen_type}")

        ytc_increment_usage(device_id)
        return {"result": result, "usage": ytc_get_usage(device_id)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("ytcopilot generate error [%s]: %s", gen_type, e)
        raise HTTPException(500, str(e))


@app.post("/ytcopilot/optimize")
async def ytcopilot_optimize(body: dict = Body(...)):
    device_id = (body.get("device_id") or "").strip()
    topic = (body.get("topic") or "").strip()
    current_title = (body.get("current_title") or "").strip()
    target = (body.get("target") or "").strip()
    style = (body.get("style") or "").strip()

    if not device_id:
        raise HTTPException(400, "device_id is required")
    if not topic:
        raise HTTPException(400, "topic is required")
    if not ytc_can_generate(device_id):
        raise HTTPException(429, "오늘 무료 생성 횟수(5회)를 모두 사용했습니다.")

    try:
        result = await ytc_optimize(topic, current_title, target, style)
        ytc_increment_usage(device_id)
        return {"result": result, "usage": ytc_get_usage(device_id)}
    except ValueError as e:
        raise HTTPException(400, str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error("ytcopilot optimize error: %s", e)
        raise HTTPException(500, str(e))
