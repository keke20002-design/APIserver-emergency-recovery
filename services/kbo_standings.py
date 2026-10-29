import logging
import re

import httpx

logger = logging.getLogger(__name__)

KBO_TEAM_RANK_URL = "https://www.koreabaseball.com/Record/TeamRank/TeamRankDaily.aspx"

# KBO 팀명 → 게임 API 단축명 통일
_TEAM_SHORT = {
    "LG": "LG", "두산": "두산", "키움": "키움", "SSG": "SSG", "KT": "KT",
    "롯데": "롯데", "한화": "한화", "삼성": "삼성", "KIA": "KIA", "NC": "NC",
    "LG트윈스": "LG", "두산베어스": "두산", "키움히어로즈": "키움",
    "SSG랜더스": "SSG", "KT위즈": "KT", "롯데자이언츠": "롯데",
    "한화이글스": "한화", "삼성라이온즈": "삼성", "KIA타이거즈": "KIA", "NC다이노스": "NC",
}


async def fetch_kbo_standings() -> list[dict]:
    """KBO 공식 사이트 HTML에서 팀 순위표를 스크래핑합니다."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://www.koreabaseball.com/",
        "Accept-Language": "ko-KR,ko;q=0.9",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
            response = await client.get(KBO_TEAM_RANK_URL)
            response.raise_for_status()
            html = response.text
        return _parse_standings_html(html)
    except httpx.HTTPError as e:
        logger.error("Failed to fetch KBO standings: %s", e)
        raise RuntimeError(f"Failed to fetch KBO standings: {e}")


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


def _parse_standings_html(html: str) -> list[dict]:
    """Parse KBO team ranking table from HTML."""
    # 테이블 본문(tbody) 추출
    tbody_match = re.search(r"<tbody>(.*?)</tbody>", html, re.DOTALL)
    if not tbody_match:
        logger.warning("KBO standings table tbody not found")
        return []

    tbody = tbody_match.group(1)
    rows = re.findall(r"<tr>(.*?)</tr>", tbody, re.DOTALL)

    standings = []
    for row in rows:
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)
        if len(cells) < 7:
            continue

        texts = [_strip_tags(c) for c in cells]
        # 컬럼 순서: 순위, 팀명, 경기, 승, 패, 무, 승률, 게임차, 최근10경기, 연속, 홈, 방문
        try:
            rank = int(texts[0])
            raw_name = texts[1]
            team = _TEAM_SHORT.get(raw_name, raw_name)
            win = int(texts[3])
            lose = int(texts[4])
            draw = int(texts[5])
            win_rate = texts[6]
        except (ValueError, IndexError):
            continue

        standings.append({
            "rank": rank,
            "team": team,
            "win": win,
            "draw": draw,
            "lose": lose,
            "win_rate": win_rate,
            "recent": [],  # KBO HTML은 순서 있는 최근폼 미제공
        })

    logger.info("Parsed %d KBO standings from KBO official site", len(standings))
    return standings
