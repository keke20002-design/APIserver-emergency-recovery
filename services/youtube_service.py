import html
import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone

import httpx

logger = logging.getLogger(__name__)

YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "")
SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"


def _date_slash(date_str: str, offset_days: int = 0) -> str:
    try:
        clean = date_str.replace("-", "")
        dt = datetime(int(clean[:4]), int(clean[4:6]), int(clean[6:8])) + timedelta(days=offset_days)
        return f"{dt.month}/{dt.day}"
    except (ValueError, IndexError):
        return date_str


def _date_dot(date_str: str) -> str:
    """YYYY-MM-DD → MM.DD (예: 2026-05-04 → 05.04)"""
    try:
        clean = date_str.replace("-", "")
        return f"{int(clean[4:6]):02d}.{int(clean[6:8]):02d}"
    except (ValueError, IndexError):
        return date_str


def _is_non_vietnamese_channel(channel_name: str) -> bool:
    return not any(0x1EA0 <= ord(c) <= 0x1EF9 for c in channel_name)


def _parse_duration(iso: str) -> str:
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso)
    if not m:
        return ""
    hours = int(m.group(1) or 0)
    minutes = int(m.group(2) or 0)
    seconds = int(m.group(3) or 0)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


async def _yt_search(
    query: str,
    max_results: int,
    published_after: str | None = None,
    published_before: str | None = None,
    order: str = "date",
    channel_id: str | None = None,
) -> list[dict]:
    params: dict = {
        "part": "snippet",
        "q": query,
        "type": "video",
        "maxResults": max_results,
        "order": order,
        "regionCode": "KR",
        "relevanceLanguage": "ko",
        "key": YOUTUBE_API_KEY,
    }
    if published_after:
        params["publishedAfter"] = published_after
    if published_before:
        params["publishedBefore"] = published_before
    if channel_id:
        params["channelId"] = channel_id
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(SEARCH_URL, params=params)
        resp.raise_for_status()
        return resp.json().get("items", [])


_HIGHLIGHT_KEYWORDS = ("하이라이트",)
_SCENE_KEYWORDS = ("주요장면", "명장면")
_TODAY_KEYWORDS = ("오늘경기", "오늘 경기")
_PREVIEW_KEYWORDS = ("프리뷰", "미리보기")
_OFFICIAL_KBO_CHANNELS = {
    "tving sports", "kbo", "kbo리그", "spotv", "spotv now", "sbs sports", "mbc sports+",
}
# 재편집/저품질 하이라이트 리업로드 채널 — 결과에서 제외
_BLOCKED_KBO_CHANNELS = {
    "스포츠 3d 하이라이트",
}

# 구단 공식 유튜브 채널 ID — 이긴 팀의 공식 H/L 영상을 우선 찾기 위해 사용.
# @handle 문자열은 다른 사람에게 넘어가거나(예: @kiatigers는 현재 영상 0개짜리 방치 채널) 바뀔 수 있어서
# 믿을 수 없다 — channelId(불변)로 직접 하드코딩하고, 실제 팀 공식 채널인지는 구독자 수로 확인해뒀다.
_TEAM_OFFICIAL_CHANNEL_ID: dict[str, str] = {
    "KIA":  "UCKp8knO8a6tSI1oaLjfd9XA",  # 기아타이거즈 KIA TIGERS (43만 구독)
    "삼성": "UCMWAku3a3h65QpLm63Jf2pw",  # LionsTV (40만 구독)
    "LG":   "UCL6QZZxb-HR4hCh_eFAnQWA",  # LGTWINSTV (33만 구독)
    "두산": "UCsebzRfMhwYfjeBIxNX1brg",  # BEARS TV (29만 구독)
    "KT":   "UCvScyjGkBUx2CJDMNAi9Twg",  # kt wiz - 위즈TV (13만 구독)
    "SSG":  "UCt8iRtgjVqm5rJHNl1TUojg",  # SSG LANDERS (23만 구독)
    "롯데": "UCAZQZdSY5_YrziMPqXi-Zfw",  # Giants TV (38만 구독)
    "한화": "UCdq4Ji3772xudYRUatdzRrg",  # Eagles TV (57만 구독)
    "NC":   "UC8_FRgynMX8wlGsU6Jh3zKg",  # NC Dinos (13만 구독)
    "키움": "UC_MA8-XEaVmvyayPzG66IKg",  # 키움히어로즈 (12만 구독)
}

# 제목 안의 "M/D", "M.D", "M월 D일" 형태 날짜 — 오늘 경기와 다른 날짜가 박혀있으면 지난 경기로 간주
_KBO_DATE_PATTERN = re.compile(r"(\d{1,2})\s*[/.]\s*(\d{1,2})|(\d{1,2})\s*월\s*(\d{1,2})\s*일")


def _kbo_date_mismatch(title: str, month: int, day: int) -> bool:
    """제목에 날짜가 있는데 오늘 경기 날짜와 하나도 안 맞으면 True (지난 경기/다른 날 경기)."""
    found = False
    for m in _KBO_DATE_PATTERN.finditer(title):
        mm = int(m.group(1) or m.group(3))
        dd = int(m.group(2) or m.group(4))
        found = True
        if mm == month and dd == day:
            return False
    return found


def _kbo_relevance_score(
    title: str, channel: str, away_team: str, home_team: str,
    date_slash: str, month: int, day: int, year: str,
) -> int:
    score = 0
    has_away = away_team in title
    has_home = home_team in title
    if has_away:
        score += 25
    if has_home:
        score += 25
    if has_away and has_home:
        score += 15
    if date_slash in title or f"{month}월 {day}일" in title or f"{month}월{day}일" in title:
        score += 25
    if any(k in title for k in _HIGHLIGHT_KEYWORDS):
        score += 30
    if any(k in title for k in _SCENE_KEYWORDS):
        score += 20
    if any(k in title for k in _TODAY_KEYWORDS):
        score += 15
    if any(k in title for k in _PREVIEW_KEYWORDS):
        score += 15
    if year in title:
        score += 10
    if channel.strip().lower() in _OFFICIAL_KBO_CHANNELS:
        score += 15
    return score


async def _search_official_highlight(
    team: str, away_team: str, home_team: str, month: int, day: int,
) -> dict | None:
    """해당 팀 공식 채널에서 오늘 경기 영상을 찾는다. 없으면 None."""
    channel_id = _TEAM_OFFICIAL_CHANNEL_ID.get(team)
    if not channel_id:
        return None

    query = f"{away_team} vs {home_team}"
    try:
        items = await _yt_search(query, 5, order="date", channel_id=channel_id)
    except Exception as e:
        logger.warning("Official channel search failed for %s: %s", team, e)
        return None
    if not items:
        return None

    video_ids = [item["id"]["videoId"] for item in items if item.get("id", {}).get("videoId")]
    if not video_ids:
        return None

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            detail_resp = await client.get(
                VIDEOS_URL,
                params={"part": "contentDetails", "id": ",".join(video_ids), "key": YOUTUBE_API_KEY},
            )
            detail_resp.raise_for_status()
            detail_items = detail_resp.json().get("items", [])
    except Exception as e:
        logger.warning("Official channel video detail fetch failed for %s: %s", team, e)
        detail_items = []

    duration_map = {
        d["id"]: _parse_duration(d["contentDetails"]["duration"])
        for d in detail_items
        if d.get("contentDetails", {}).get("duration")
    }

    for item in items:
        vid = item.get("id", {}).get("videoId", "")
        if not vid:
            continue
        snippet = item.get("snippet", {})
        title = snippet.get("title", "")
        # 팀 이름이 모두 있고, 오늘과 다른 날짜가 박혀있지 않은 영상만 인정
        if away_team not in title or home_team not in title:
            continue
        if _kbo_date_mismatch(title, month, day):
            continue
        return {
            "video_id": vid,
            "title": title,
            "channel_name": snippet.get("channelTitle", ""),
            "duration": duration_map.get(vid, ""),
        }
    return None


async def search_kbo_videos(
    away_team: str,
    home_team: str,
    date_str: str,
    status: str = "",
    max_results: int = 20,
    winner_team: str | None = None,
) -> list[dict]:
    """경기 상태별로 검색어 1개만 사용 — 관련성 낮은 영상은 걸러내고 점수순 정렬.

    할당량 절약을 위해 여러 검색어를 합치거나 넓은 fallback 검색을 쓰지 않는다.
    (빈 결과 처리/재검색 주기는 main.py의 /videos 캐시 로직에서 담당)
    경기 종료 + 승리팀을 알 때는 승리팀 공식 채널에서 H/L 영상도 별도로 찾아 최상단에 둔다.
    """
    if status == "경기 취소":
        return []

    date_slash = _date_slash(date_str)
    try:
        clean = date_str.replace("-", "")
        year, month, day = clean[:4], int(clean[4:6]), int(clean[6:8])
    except (ValueError, IndexError):
        year, month, day = str(datetime.now().year), 0, 0

    if status == "경기 종료":
        # 날짜까지 검색어에 넣으면 YouTube 검색이 구단 채널 영상 한두 개로
        # 과도하게 좁아진다. 날짜 일치 여부는 아래 제목 필터가 담당한다.
        query = f"{away_team} {home_team} 하이라이트"
    elif status == "경기 중":
        query = f"{away_team} {home_team} 오늘 경기 주요장면"
    else:  # 경기 전 등
        query = f"{away_team} {home_team} {date_slash} 프리뷰"

    logger.info("YouTube search query: %s", query)
    items = await _yt_search(query, max_results)

    team_officials: list[dict] = []
    if status == "경기 종료":
        official_results = await asyncio.gather(*[
            _search_official_highlight(team, away_team, home_team, month, day)
            for team in dict.fromkeys((away_team, home_team))
            if team in _TEAM_OFFICIAL_CHANNEL_ID
        ])
        team_officials = [video for video in official_results if video]

    scored: list[tuple[int, dict]] = []
    if items:
        video_ids = [item["id"]["videoId"] for item in items if item.get("id", {}).get("videoId")]

        async with httpx.AsyncClient(timeout=10.0) as client:
            detail_resp = await client.get(
                VIDEOS_URL,
                params={"part": "contentDetails", "id": ",".join(video_ids), "key": YOUTUBE_API_KEY},
            )
            detail_resp.raise_for_status()
            detail_items = detail_resp.json().get("items", [])

        duration_map = {
            d["id"]: _parse_duration(d["contentDetails"]["duration"])
            for d in detail_items
            if d.get("contentDetails", {}).get("duration")
        }

        for item in items:
            vid = item.get("id", {}).get("videoId", "")
            if not vid:
                continue
            snippet = item.get("snippet", {})
            title = snippet.get("title", "")
            channel = snippet.get("channelTitle", "")

            # 두 팀 이름이 모두 제목에 있어야 함 — 한 팀만 겹치는 다른 매치업 영상(예: KIA vs LG)이
            # 딸려오는 걸 막기 위해 OR가 아니라 AND로 검사
            if away_team not in title or home_team not in title:
                continue
            if channel.strip().lower() in _BLOCKED_KBO_CHANNELS:
                continue
            # 제목에 오늘과 다른 날짜가 박혀있으면 지난 경기 영상 → 제외
            if _kbo_date_mismatch(title, month, day):
                continue

            score = _kbo_relevance_score(title, channel, away_team, home_team, date_slash, month, day, year)
            scored.append((score, {
                "video_id": vid,
                "title": title,
                "channel_name": channel,
                "duration": duration_map.get(vid, ""),
            }))

    scored.sort(key=lambda x: x[0], reverse=True)
    results = [v for _, v in scored]

    broadcaster_officials = [
        video for video in results
        if video["channel_name"].strip().lower() in _OFFICIAL_KBO_CHANNELS
    ]
    other_results = [
        video for video in results
        if video["channel_name"].strip().lower() not in _OFFICIAL_KBO_CHANNELS
    ]
    preferred = broadcaster_officials + team_officials
    diverse_preferred: list[dict] = []
    repeated_preferred: list[dict] = []
    seen_channels: set[str] = set()
    seen_ids: set[str] = set()
    for video in preferred:
        if video["video_id"] in seen_ids:
            continue
        seen_ids.add(video["video_id"])
        channel = video["channel_name"].strip().lower()
        if channel in seen_channels:
            repeated_preferred.append(video)
        else:
            seen_channels.add(channel)
            diverse_preferred.append(video)
    results = diverse_preferred + repeated_preferred + [
        video for video in other_results if video["video_id"] not in seen_ids
    ]

    return results


# ── 국제대회(아시안게임 등) 경기 영상 — SPOTV 공식 채널 ─────────────────────────────
# 제목 형식: "[야구] 일본 vs 중국 3분 하이라이트 | 아이치-나고야 아시안게임"
#            "[야구] 대만 vs 대한민국 곽빈 주요장면 | 아이치-나고야 아시안게임"
_SPOTV_CHANNEL_ID = "UCtm_QoN2SIxwCE-59shX7Qg"

# Naver 팀명 → 영상 제목에서 쓰일 수 있는 표기들 (첫 번째가 SPOTV 표기, 검색어에도 사용).
# 예: Naver는 '차이니스 타이베이'인데 SPOTV는 '대만' — 그대로 비교하면 항상 탈락한다.
# 등록 안 된 팀은 Naver 표기 그대로 비교한다.
_INTL_TEAM_ALIASES: dict[str, tuple[str, ...]] = {
    "대한민국": ("대한민국", "한국"),
    "차이니스 타이베이": ("대만", "타이완", "타이베이", "차이니스 타이베이"),
    "중국": ("중국",),
    "일본": ("일본",),
    "홍콩": ("홍콩",),
    "태국": ("태국",),
    "필리핀": ("필리핀",),
    "팔레스타인": ("팔레스타인",),
}


def _intl_aliases(team: str) -> tuple[str, ...]:
    return _INTL_TEAM_ALIASES.get(team, (team,))


def _kst_window(date_str: str) -> tuple[str | None, str | None]:
    """경기 당일 00:00 KST ~ 다음날 23:59 KST 를 UTC ISO로 (하이라이트는 경기 직후~다음날 아침에 올라옴).
    같은 두 팀이 대회 중 다시 붙어도 다른 날 영상이 섞이지 않게 하는 용도."""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return None, None
    start = d - timedelta(hours=9)                         # 당일 00:00 KST
    end = d + timedelta(days=2) - timedelta(hours=9, seconds=1)  # 다음날 23:59:59 KST
    return start.strftime("%Y-%m-%dT%H:%M:%SZ"), end.strftime("%Y-%m-%dT%H:%M:%SZ")


# 방송사·공식 채널 — 같은 등급 안에서 개인/클립 채널보다 먼저 보여준다 (SPOTV가 최우선)
_INTL_OFFICIAL_CHANNEL_HINTS = (
    "tving", "kbo", "sbs", "스브스", "mbc", "엠빅", "kbs", "tvchosun", "tv조선",
    "스포츠머그", "연합뉴스", "jtbc",
)


def _intl_title_match(title: str, away_team: str, home_team: str) -> bool:
    """국제대회 경기 영상 제목 판정.

    - 야구 영상이어야 함('야구' 또는 '⚾'): 같은 대회의 농구/배구 등 다른 종목 제외.
    - 두 팀이 별칭 기준으로 모두 제목에 있으면 인정(홈/원정 순서 무관).
    - 한국 방송사는 '대만전 5:0 완승'처럼 한국을 생략하는 경우가 많아, 한국 경기라면 상대팀만 있어도 인정.
    - LIVE 중계는 제외.
    """
    if "야구" not in title and "⚾" not in title:
        return False
    if "LIVE" in title.upper() or "라이브" in title:
        return False
    away_names, home_names = _intl_aliases(away_team), _intl_aliases(home_team)
    has_away = any(n in title for n in away_names)
    has_home = any(n in title for n in home_names)
    if has_away and has_home:
        return True
    if away_team == "대한민국":
        return has_home
    if home_team == "대한민국":
        return has_away
    return False


def _intl_rank(title: str, channel: str) -> tuple[int, int]:
    """(내용 등급, 채널 등급) — 작을수록 먼저. 하이라이트 > 주요장면 > 기타, 같은 등급에서는 SPOTV > 방송사 > 그 외."""
    if "하이라이트" in title:
        tier = 0
    elif "주요장면" in title or "명장면" in title:
        tier = 1
    else:
        tier = 2
    ch = channel.strip().lower()
    if ch == "spotv":
        chan = 0
    elif any(h in ch for h in _INTL_OFFICIAL_CHANNEL_HINTS):
        chan = 1
    else:
        chan = 2
    return tier, chan


async def search_international_videos(
    away_team: str,
    home_team: str,
    date_str: str,
    status: str = "",
    competition_name: str = "",
    max_results: int = 6,
) -> list[dict]:
    """국제대회(아시안게임 등) 경기 영상.

    1) SPOTV 채널 안에서 두 팀 영상을 먼저 찾고,
    2) 3개에 못 미치면 채널 제한 없는 검색으로 다른 채널(방송사 하이라이트 등)을 보완한다.
    경기 당일~다음날(KST) 업로드분만 인정해 같은 두 팀의 다른 날 경기 영상이 섞이지 않게 한다.
    """
    if status == "경기 취소":
        return []

    away_names = _intl_aliases(away_team)
    home_names = _intl_aliases(home_team)
    after, before = _kst_window(date_str)

    # 경기 당일이 아직 시작 전(내일 경기 등)이면 영상이 있을 수 없다. publishedAfter가 미래면
    # YouTube가 400을 반환하므로 호출하지 않는다 (할당량/에러 로그 절약).
    if after and datetime.strptime(after, "%Y-%m-%dT%H:%M:%SZ") > datetime.now(timezone.utc).replace(tzinfo=None):
        return []

    picked: dict[str, dict] = {}

    def collect(items: list[dict]) -> None:
        for item in items:
            vid = item.get("id", {}).get("videoId", "")
            snippet = item.get("snippet", {})
            title = html.unescape(snippet.get("title", ""))
            channel = snippet.get("channelTitle", "")
            if not vid or vid in picked:
                continue
            if channel.strip().lower() in _BLOCKED_KBO_CHANNELS:
                continue
            if not _intl_title_match(title, away_team, home_team):
                continue
            picked[vid] = {"video_id": vid, "title": title, "channel_name": channel}

    q_spotv = f"[야구] {away_names[0]} vs {home_names[0]}"
    logger.info("International video search (SPOTV): %s [%s ~ %s]", q_spotv, after, before)
    collect(await _yt_search(
        q_spotv, 10, published_after=after, published_before=before,
        order="date", channel_id=_SPOTV_CHANNEL_ID,
    ))

    if len(picked) < 3:
        q_general = " ".join(x for x in (away_names[0], home_names[0], "야구 하이라이트", competition_name) if x)
        logger.info("International video search (general): %s", q_general)
        collect(await _yt_search(
            q_general, 15, published_after=after, published_before=before, order="relevance",
        ))

    if not picked:
        return []

    async with httpx.AsyncClient(timeout=10.0) as client:
        detail_resp = await client.get(
            VIDEOS_URL,
            params={"part": "contentDetails", "id": ",".join(picked), "key": YOUTUBE_API_KEY},
        )
        detail_resp.raise_for_status()
        for d in detail_resp.json().get("items", []):
            dur = d.get("contentDetails", {}).get("duration")
            if dur and d["id"] in picked:
                picked[d["id"]]["duration"] = _parse_duration(dur)

    ranked = sorted(picked.values(), key=lambda v: _intl_rank(v["title"], v["channel_name"]))  # 안정 정렬
    return [{**v, "duration": v.get("duration", "")} for v in ranked[:max_results]]



async def _build_video_results(
    items: list[dict],
    filter_vietnamese: bool = True,
) -> list[dict]:
    if not items:
        return []
    video_ids = [item["id"]["videoId"] for item in items if item.get("id", {}).get("videoId")]
    async with httpx.AsyncClient(timeout=10.0) as client:
        detail_resp = await client.get(
            VIDEOS_URL,
            params={"part": "contentDetails", "id": ",".join(video_ids), "key": YOUTUBE_API_KEY},
        )
        detail_resp.raise_for_status()
        detail_items = detail_resp.json().get("items", [])
    duration_map = {
        d["id"]: _parse_duration(d["contentDetails"]["duration"])
        for d in detail_items
        if d.get("contentDetails", {}).get("duration")
    }
    results = []
    for item in items:
        vid = item.get("id", {}).get("videoId", "")
        if not vid:
            continue
        snippet = item.get("snippet", {})
        channel = snippet.get("channelTitle", "")
        if filter_vietnamese and not _is_non_vietnamese_channel(channel):
            logger.info("Filtered out Vietnamese channel: %s", channel)
            continue
        results.append({
            "video_id": vid,
            "title": snippet.get("title", ""),
            "channel_name": channel,
            "duration": duration_map.get(vid, ""),
        })
    return results


def _date_range(date_str: str, days: int = 5) -> tuple[str | None, str | None]:
    try:
        match_dt = datetime.strptime(date_str, "%Y-%m-%d")
        return (
            match_dt.strftime("%Y-%m-%dT00:00:00Z"),
            (match_dt + timedelta(days=days)).strftime("%Y-%m-%dT23:59:59Z"),
        )
    except ValueError:
        return None, None


async def search_lck_videos(
    away_team: str,
    home_team: str,
    away_code: str,
    home_code: str,
    date_str: str,
    max_results: int = 5,
) -> list[dict]:
    after, before = _date_range(date_str)

    # 1차: 원정 vs 홈 하이라이트 2026 LCK (공식 채널 포맷)
    q1 = f"{away_code} vs {home_code} 하이라이트 2026 LCK"
    logger.info("LCK video search: %s", q1)
    items = await _yt_search(q1, max_results, published_after=after, published_before=before)

    # 2차: 홈 vs 원정 순서
    if not items:
        q2 = f"{home_code} vs {away_code} 하이라이트 2026 LCK"
        logger.info("LCK video search fallback: %s", q2)
        items = await _yt_search(q2, max_results, published_after=after, published_before=before)

    # 3차: 날짜 범위 없이
    if not items:
        q3 = f"{away_code} vs {home_code} 하이라이트 2026 LCK"
        logger.info("LCK video search final: %s", q3)
        items = await _yt_search(q3, max_results)

    return await _build_video_results(items, filter_vietnamese=True)


async def search_ewc_videos(
    away_team: str,
    home_team: str,
    away_code: str,
    home_code: str,
    date_str: str,
    max_results: int = 5,
) -> list[dict]:
    after, before = _date_range(date_str)

    # 1차: away vs home Road to EWC 2026
    q1 = f"{away_code} vs {home_code} Road to EWC 2026"
    logger.info("EWC video search: %s", q1)
    items = await _yt_search(q1, max_results, published_after=after, published_before=before)

    # 2차: home vs away 순서
    if not items:
        q2 = f"{home_code} vs {away_code} Road to EWC 2026"
        logger.info("EWC video search fallback: %s", q2)
        items = await _yt_search(q2, max_results, published_after=after, published_before=before)

    # 3차: Highlights 키워드 추가, 날짜 범위 없이
    if not items:
        q3 = f"{away_code} vs {home_code} Road to EWC 2026 Highlights"
        logger.info("EWC video search final: %s", q3)
        items = await _yt_search(q3, max_results)

    return await _build_video_results(items, filter_vietnamese=True)


async def search_epl_videos(
    home_team: str,
    away_team: str,
    date_str: str,
    max_results: int = 6,
) -> list[dict]:
    """EPL match highlight search on YouTube.

    Each fallback query is tried independently — a transient failure (a
    timeout, a momentary non-2xx from YouTube) on one query must not sink
    the other two, which is what a single try/except around the whole
    function would do. Only if all three attempts fail do we return an
    empty list, which the client already renders as "no highlights"
    rather than an error.
    """
    after, before = _date_range(date_str, days=7)

    async def _try(query: str, **kwargs) -> list[dict]:
        try:
            return await _yt_search(query, max_results, **kwargs)
        except Exception as e:
            logger.warning("EPL video search attempt failed (%s): %s", query, e)
            return []

    q1 = f"{home_team} vs {away_team} highlights Premier League"
    logger.info("EPL video search: %s", q1)
    items = await _try(
        q1, published_after=after, published_before=before, order="relevance"
    )

    if not items:
        q2 = f"{away_team} vs {home_team} highlights Premier League"
        logger.info("EPL video search fallback: %s", q2)
        items = await _try(q2, published_after=after, order="relevance")

    if not items:
        q3 = f"{home_team} {away_team} Premier League highlights"
        logger.info("EPL video search final: %s", q3)
        items = await _try(q3, order="relevance")

    try:
        return await _build_video_results(items, filter_vietnamese=False)
    except Exception as e:
        logger.warning("EPL video result build failed: %s", e)
        return []


async def search_recipe_videos(recipe_name: str, max_results: int = 6) -> list[dict]:
    """한식 레시피 YouTube 영상 검색 — 관련성 우선 검색 후 조회수 순 재정렬, 한국어 우선.

    order="viewCount"로 직접 검색하면 텍스트 관련성보다 조회수가 우선시되어,
    "레시피"/"만들기" 같은 흔한 키워드만 걸친 무관한 인기 영상이 섞여 나온다.
    relevance로 후보를 넉넉히 뽑은 뒤 실제 조회수로 재정렬해 관련성과 인기를 모두 확보한다.
    """
    candidate_pool = max_results * 3
    q = f"{recipe_name} 만들기 레시피"
    items = await _yt_search(q, candidate_pool, order="relevance")
    if not items:
        # 한국어 폴백: 레시피 단어만으로 재시도
        items = await _yt_search(f"{recipe_name} 레시피", candidate_pool, order="relevance")
    if not items:
        return []

    video_ids = [item["id"]["videoId"] for item in items if item.get("id", {}).get("videoId")]
    view_counts: dict[str, int] = {}
    if video_ids:
        async with httpx.AsyncClient(timeout=10.0) as client:
            stats_resp = await client.get(
                VIDEOS_URL,
                params={"part": "statistics", "id": ",".join(video_ids), "key": YOUTUBE_API_KEY},
            )
            stats_resp.raise_for_status()
            for d in stats_resp.json().get("items", []):
                try:
                    view_counts[d["id"]] = int(d.get("statistics", {}).get("viewCount", 0))
                except (KeyError, ValueError):
                    view_counts[d["id"]] = 0

    results = []
    for item in items:
        vid = item.get("id", {}).get("videoId", "")
        if not vid:
            continue
        snippet = item.get("snippet", {})
        thumbs = snippet.get("thumbnails", {})
        thumbnail = (
            thumbs.get("medium", {}).get("url")
            or thumbs.get("default", {}).get("url")
            or ""
        )
        results.append({
            "videoId": vid,
            "title": snippet.get("title", ""),
            "channelTitle": snippet.get("channelTitle", ""),
            "thumbnail": thumbnail,
            "_viewCount": view_counts.get(vid, 0),
        })

    results.sort(key=lambda r: r["_viewCount"], reverse=True)
    for r in results:
        del r["_viewCount"]
    return results[:max_results]


YASSONG_CHANNEL_ID = "UC0KYcMjlFq08UjK2cWkB9Cg"  # '야쏭' — KBO 응원가 전문 채널


async def search_cheer_video(query: str) -> str | None:
    """응원가 검색어로 가장 관련성 높은 첫 번째 영상 ID 반환. 없으면 None.

    '야쏭' 채널의 응원가를 우선으로 찾고, 그 채널에 없으면 일반 검색으로 보충한다.
    """
    items = await _yt_search(query, max_results=1, order="relevance", channel_id=YASSONG_CHANNEL_ID)
    if items:
        return items[0].get("id", {}).get("videoId")

    five_years_ago = (datetime.utcnow() - timedelta(days=365 * 5)).strftime("%Y-%m-%dT00:00:00Z")
    items = await _yt_search(query, max_results=1, order="relevance", published_after=five_years_ago)
    if not items:
        # 5년 내 결과 없으면 기간 제한 없이 재시도
        items = await _yt_search(query, max_results=1, order="relevance")
    if not items:
        return None
    return items[0].get("id", {}).get("videoId")


async def search_bible_videos(query: str, max_results: int = 6) -> list[dict]:
    """Bible Hub 앱용 범용 영상 검색 (설교/찬양/찬송가/QT 공용).

    호출측(Flutter)이 "창세기 1장 설교" 같은 완성된 검색어를 넘겨준다.
    relevance로 검색 후 duration/thumbnail을 붙여 반환한다.
    """
    items = await _yt_search(query, max_results, order="relevance")
    if not items:
        return []

    video_ids = [item["id"]["videoId"] for item in items if item.get("id", {}).get("videoId")]
    async with httpx.AsyncClient(timeout=10.0) as client:
        detail_resp = await client.get(
            VIDEOS_URL,
            params={"part": "contentDetails", "id": ",".join(video_ids), "key": YOUTUBE_API_KEY},
        )
        detail_resp.raise_for_status()
        detail_items = detail_resp.json().get("items", [])
    duration_map = {
        d["id"]: _parse_duration(d["contentDetails"]["duration"])
        for d in detail_items
        if d.get("contentDetails", {}).get("duration")
    }

    results = []
    for item in items:
        vid = item.get("id", {}).get("videoId", "")
        if not vid:
            continue
        snippet = item.get("snippet", {})
        thumbs = snippet.get("thumbnails", {})
        thumbnail = (
            thumbs.get("medium", {}).get("url")
            or thumbs.get("default", {}).get("url")
            or ""
        )
        results.append({
            "video_id": vid,
            "title": snippet.get("title", ""),
            "channel_name": snippet.get("channelTitle", ""),
            "duration": duration_map.get(vid, ""),
            "thumbnail": thumbnail,
        })
    return results
