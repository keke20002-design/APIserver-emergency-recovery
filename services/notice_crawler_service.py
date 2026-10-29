"""상품권 발행 대행 플랫폼 5종의 공지사항 크롤러.

수백 개 지자체 사이트를 개별 크롤링하는 대신, 발행 공지가 모이는 상위
플랫폼 5곳의 공지사항만 수집한다. 공지 원문(제목/링크/날짜)만 수집해
crawled_notices에 저장하고, issuance_schedules로의 구조화는 하지 않는다 -
공지마다 형식이 제각각이고(예: 서울시는 한 공지에 자치구 4곳을 같이 적음)
할인율/인당한도/정확한 발행시각을 텍스트에서 안정적으로 자동 추출하기
어렵기 때문. 구조화는 운영자가 POST/PUT /admin/schedules로 직접 하거나,
나중에 gemini_service 같은 걸로 텍스트 추출을 붙이는 후속 작업으로 둔다.

실측 확인(2026-09):
    동백전(busandong100.kr): JS가 호출하는 내부 JSON API 발견, 정식 사용.
    경기지역화폐(gmoney.or.kr): 정적 서버렌더 HTML 테이블, 정식 사용.
    인천e음/서울페이+/CHAK: 공지 게시판이 JS 렌더(그리드 로딩) 또는 공식
    게시판 URL 자체를 못 찾음 - 스텁으로 남겨두고 빈 리스트 반환.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; locakcurapp-crawler/1.0)"}


async def _fetch_dongbaekjeon(client: httpx.AsyncClient) -> list[dict]:
    """부산 동백전 공지사항 (busandong100.kr, KBoard 내부 API)."""
    response = await client.post(
        "https://busandong100.kr/api/board/boardList",
        json={
            "boardType": "NOTICE",
            "pageNum": 1,
            "pageSize": 15,
            "searchKeyword": "",
            "searchTarget": "",
        },
        headers={**_HEADERS, "Content-Type": "application/json"},
    )
    response.raise_for_status()
    items = response.json().get("resultData", {}).get("resultList", [])

    notices = []
    for item in items:
        published = _parse_date(item.get("regDate"), fmt="%Y.%m.%d")
        notices.append(
            {
                "platform": "dongbaekjeon",
                "notice_id": str(item["noticeId"]),
                "title": item["title"],
                "url": f"https://busandong100.kr/cs/noticeDesc?seq={item['noticeId']}",
                "published_date": published,
                "excerpt": _strip_html(item.get("contents"))[:500],
            }
        )
    return notices


async def _fetch_gyeonggi(client: httpx.AsyncClient) -> list[dict]:
    """경기지역화폐 공지사항 (gmoney.or.kr, 서버 렌더 HTML 테이블)."""
    response = await client.get(
        "https://gmoney.or.kr/base/board/list",
        params={"boardManagementNo": 1, "menuLevel": 2, "menuNo": 33},
        headers=_HEADERS,
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    notices = []
    for row in soup.select("table tbody tr"):
        link = row.select_one("td.tit a[href]")
        date_cell = row.select_one("td.date")
        if not link:
            continue
        href = link["href"]
        match = re.search(r"boardNo=(\d+)", href)
        notice_id = match.group(1) if match else href
        notices.append(
            {
                "platform": "gyeonggi",
                "notice_id": notice_id,
                "title": link.get_text(strip=True),
                "url": href if href.startswith("http") else f"https://gmoney.or.kr{href}",
                "published_date": _parse_date(
                    date_cell.get_text(strip=True) if date_cell else None,
                    fmt="%Y.%m.%d",
                ),
                "excerpt": None,
            }
        )
    return notices


async def _fetch_seoul_pay(client: httpx.AsyncClient) -> list[dict]:
    # seoulpay.seoul.go.kr 등 후보 도메인 확인해봤으나 DNS 자체가 안 뜸 -
    # 공식 공지사항 게시판 URL을 아직 못 찾음.
    logger.info("서울페이+ 공지사항 게시판 URL 미확인 - 스텁, 빈 목록 반환")
    return []


async def _fetch_chak(client: httpx.AsyncClient) -> list[dict]:
    # localpay.komscochak.com은 React/Vue 계열 JS SPA라 httpx+BeautifulSoup로는
    # 빈 껍데기 HTML만 받아짐 - 실제 콘텐츠 수집은 Playwright 같은 헤드리스
    # 브라우저가 필요. komsco.com 자체 공지사항(/kor/article/notice)은 있지만
    # 지역상품권이 아니라 조폐공사 전체 공지(입찰/채용 등)라 관련성 낮음.
    logger.info("지역상품권 chak - JS SPA라 정적 크롤링 불가, 스텁, 빈 목록 반환")
    return []


async def _fetch_incheon_eum(client: httpx.AsyncClient) -> list[dict]:
    # incheoneum.or.kr/info/notice도 그리드가 JS로 로딩됨(id="grid-loading").
    # 내부 AJAX 엔드포인트를 동백전처럼 못 찾음 - 페이지 자체가 React 기반이라
    # 요청이 REST가 아니라 GraphQL일 가능성도 있어 추가 조사 필요.
    logger.info("인천e음 공지사항은 JS 그리드 로딩 - 내부 API 미확인, 스텁, 빈 목록 반환")
    return []


# 대구로페이/CHAK/광주상생카드(kjbank)도 검토했으나 전부 JS SPA이거나
# (대구로페이, CHAK) 특정 상품권 공지만 골라내기 어려운 종합 사이트라(광주은행)
# 이번 범위에서는 제외. 헤드리스 브라우저(Playwright) 도입은 서비스 운영
# 추이를 보고 추후 재논의하기로 함(2026-09-16, 사용자 결정) - 그 전까지는
# 이 5개 스텁 플랫폼의 발행 일정은 /admin/schedules로 수기 등록한다.


_SOURCES = {
    "dongbaekjeon": _fetch_dongbaekjeon,
    "gyeonggi": _fetch_gyeonggi,
    "seoul_pay": _fetch_seoul_pay,
    "chak": _fetch_chak,
    "incheon_eum": _fetch_incheon_eum,
}

# 관리자 대시보드 "크롤링 대상" 탭에 그대로 노출되는 메타데이터.
# implemented=False인 곳은 실제로 크롤링 안 하고 있다는 뜻 - 운영자가 직접
# 사이트를 열어보고 구조가 바뀌었거나 더 나은 URL을 찾으면 알려줄 수 있게
# notice_url을 실제 확인한 값 그대로 적어둔다(추측 URL 아님).
PLATFORM_INFO = [
    {
        "key": "dongbaekjeon",
        "label": "부산 동백전",
        "site_url": "https://busandong100.kr/",
        "notice_url": "https://busandong100.kr/cs/notice",
        "implemented": True,
        "note": "내부 JSON API(POST /api/board/boardList) 역추적해서 사용 중",
    },
    {
        "key": "gyeonggi",
        "label": "경기지역화폐",
        "site_url": "https://gmoney.or.kr/",
        "notice_url": "https://gmoney.or.kr/base/board/list?boardManagementNo=1&menuLevel=2&menuNo=33",
        "implemented": True,
        "note": "정적 서버렌더 HTML 테이블 그대로 파싱",
    },
    {
        "key": "seoul_pay",
        "label": "서울페이+ (서울사랑상품권)",
        "site_url": "https://news.seoul.go.kr/economy",
        "notice_url": None,
        "implemented": False,
        "note": "자치구별 발행 일정은 앱 안 공지 또는 각 자치구(25곳) 홈페이지/SNS에 개별 게시되는 "
        "구조라 서울시 전체를 커버하는 공식 RSS/게시판이 없음. news.seoul.go.kr에 RSS는 있지만 "
        "(예: /economy/archives/category/.../seoulpay_c1/about-seoulpay/feed) 정기 발행 일정이 "
        "아니라 '가입방법' 같은 정적 안내글뿐. 앱 내부 API는 금융앱 인증서 핀닝 때문에 리버스엔지니어링 "
        "난이도가 높아 보류 결정(2026-09-17, 사용자 결정) - 당분간 /admin/schedules로 수기 등록",
    },
    {
        "key": "chak",
        "label": "지역상품권 CHAK (한국조폐공사)",
        "site_url": "https://localpay.komscochak.com/",
        "notice_url": None,
        "implemented": False,
        "note": "React/Vue 계열 JS SPA라 정적 크롤링 불가 (Playwright 필요, 보류 중). "
        "komsco.com 자체 공지사항(/kor/article/notice)은 있지만 조폐공사 전체 공지라 관련성 낮음",
    },
    {
        "key": "incheon_eum",
        "label": "인천e음",
        "site_url": "https://www.incheoneum.or.kr/",
        "notice_url": "https://www.incheoneum.or.kr/info/notice",
        "implemented": False,
        "note": "게시판이 JS로 그리드 로딩(id=grid-loading) - 내부 AJAX 엔드포인트 미확인",
    },
    # 아래 3개는 5대 플랫폼에는 안 넣었지만 검토했던 후보 - 참고용.
    {
        "key": "daegu_ro_pay",
        "label": "대구로페이 (검토했으나 미채택)",
        "site_url": "https://daeguro.co.kr/",
        "notice_url": None,
        "implemented": False,
        "note": "React/Vue JS SPA - 서버가 빈 껍데기 HTML만 반환",
    },
    {
        "key": "gwangju_card",
        "label": "광주상생카드 (검토했으나 미채택)",
        "site_url": "https://www.kjbank.com/",
        "notice_url": None,
        "implemented": False,
        "note": "광주은행 전체 사이트라 상생카드 전용 공지만 골라내기 어려움",
    },
    {
        "key": "beple_pay",
        "label": "비플페이 (검토했으나 미채택)",
        "site_url": "https://www.beplepay.co.kr/",
        "notice_url": None,
        "implemented": False,
        "note": "접속은 되나 SSL 인증서가 도메인과 안 맞음(인증서 오류) - 신뢰성 확인 필요",
    },
]


def _parse_date(raw: str | None, fmt: str) -> str | None:
    if not raw:
        return None
    try:
        return datetime.strptime(raw.strip(), fmt).date().isoformat()
    except ValueError:
        return None


def _strip_html(html: str | None) -> str:
    if not html:
        return ""
    return BeautifulSoup(html, "html.parser").get_text(" ", strip=True)


async def crawl_all_platforms() -> list[dict]:
    """5개 플랫폼 공지사항을 전부 수집 (플랫폼 하나 실패해도 나머지는 계속).

    오늘 날짜(Asia/Seoul 기준) 이전에 등록된 공지는 제외한다 - 게시판마다
    페이지당 10~15건씩 가져오는데, 그중 몇 달 전 공지까지 섞여 있으면
    관리자 대시보드의 '미검토 공지' 큐가 오래된 글로 계속 쌓이기 때문.
    날짜를 못 읽은 공지(published_date가 None)는 실수로 놓치지 않도록
    일단 포함시킨다.
    """
    cutoff = (datetime.now(ZoneInfo("Asia/Seoul")).date() - timedelta(days=30)).isoformat()
    notices: list[dict] = []
    async with httpx.AsyncClient(timeout=20.0) as client:
        for platform, fetch_fn in _SOURCES.items():
            try:
                platform_notices = await fetch_fn(client)
                before = len(platform_notices)
                platform_notices = [
                    n for n in platform_notices
                    if n.get("published_date") is None or n["published_date"] >= cutoff
                ]
                skipped = before - len(platform_notices)
                notices.extend(platform_notices)
                logger.info(
                    "%s: 공지 %d건 수집 (최근 30일 이전 %d건 제외)",
                    platform, len(platform_notices), skipped,
                )
            except Exception as e:
                logger.error("%s 크롤링 실패: %s", platform, e)
    return notices
