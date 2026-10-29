"""KOSPI/KOSDAQ 주식 스크리닝 — FDR 기반 10일선·거래량·캔들패턴 분석."""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

import pytz

logger = logging.getLogger(__name__)
KST = pytz.timezone("Asia/Seoul")

# 관심종목 — KOSPI 대형/중형 + KOSDAQ 중소형 혼합
WATCHLIST: list[tuple[str, str]] = [
    # KOSPI
    ("005930", "삼성전자"),
    ("000660", "SK하이닉스"),
    ("005380", "현대자동차"),
    ("035420", "NAVER"),
    ("035720", "카카오"),
    ("051910", "LG화학"),
    ("068270", "셀트리온"),
    ("005490", "POSCO홀딩스"),
    ("096770", "SK이노베이션"),
    ("042700", "한미반도체"),
    ("028260", "삼성물산"),
    ("009150", "삼성전기"),
    ("034020", "두산에너빌리티"),
    ("011200", "현대글로비스"),
    ("090430", "아모레퍼시픽"),
    ("000100", "유한양행"),
    ("010140", "삼성중공업"),
    ("003550", "LG"),
    ("298040", "효성중공업"),
    ("009540", "한국조선해양"),
    ("032830", "삼성생명"),
    ("006400", "삼성SDI"),
    ("030200", "KT"),
    ("017670", "SK텔레콤"),
    ("015760", "한국전력"),
    # KOSDAQ
    ("036570", "엔씨소프트"),
    ("251270", "넷마블"),
    ("041510", "에스엠엔터테인먼트"),
    ("352820", "하이브"),
    ("263750", "펄어비스"),
    ("293490", "카카오게임즈"),
    ("112040", "위메이드"),
    ("095660", "네오위즈"),
    ("086520", "에코프로"),
    ("247540", "에코프로비엠"),
    ("003230", "삼양식품"),
    ("145020", "휴젤"),
    ("326030", "SK바이오팜"),
    ("214150", "클래시스"),
    ("009420", "한올바이오파마"),
    ("166090", "하나머티리얼즈"),
    ("196170", "알테오젠"),
    ("058470", "리노공업"),
    ("095720", "웹젠"),
    ("039490", "키움증권"),
    ("067160", "아프리카TV"),
    ("018260", "삼성에스디에스"),
    ("161390", "한국타이어앤테크놀로지"),
    ("000720", "현대건설"),
]


def _fetch_sync(ticker: str, days: int = 40):
    """FDR로 단일 종목 OHLCV 조회 (동기). 실패 시 None 반환."""
    try:
        import FinanceDataReader as fdr
        end = datetime.now(KST).date()
        start = end - timedelta(days=days + 15)
        df = fdr.DataReader(ticker, start=str(start), end=str(end))
        if df is None or len(df) < 10:
            return None
        return df.tail(days)
    except Exception as e:
        logger.debug("FDR failed for %s: %s", ticker, e)
        return None


def _hammer(df) -> bool:
    """망치형: 긴 아랫꼬리, 작은 몸통, 양봉."""
    if len(df) < 1:
        return False
    r = df.iloc[-1]
    body = abs(float(r["Close"]) - float(r["Open"]))
    lower = float(min(r["Close"], r["Open"])) - float(r["Low"])
    upper = float(r["High"]) - float(max(r["Close"], r["Open"]))
    if body < 1:
        return False
    return lower >= 2 * body and upper <= body * 0.5 and float(r["Close"]) > float(r["Open"])


def _morning_star(df) -> bool:
    """샛별형: 장대음봉 → 도지/단봉 → 장대양봉."""
    if len(df) < 3:
        return False
    d1, d2, d3 = df.iloc[-3], df.iloc[-2], df.iloc[-1]
    d1_drop = float(d1["Open"]) - float(d1["Close"])
    if d1_drop <= 0:
        return False
    d1_big_bear = d1_drop / float(d1["Open"]) > 0.015
    d2_small = abs(float(d2["Close"]) - float(d2["Open"])) < d1_drop * 0.35
    d3_bull_close = float(d3["Close"]) > (float(d1["Open"]) + float(d1["Close"])) / 2
    d3_bullish = float(d3["Close"]) > float(d3["Open"])
    return d1_big_bear and d2_small and d3_bull_close and d3_bullish


def _engulfing(df) -> bool:
    """상승장악형: 전일 음봉을 오늘 양봉이 완전히 감쌈."""
    if len(df) < 2:
        return False
    prev, curr = df.iloc[-2], df.iloc[-1]
    prev_bear = float(prev["Close"]) < float(prev["Open"])
    curr_bull = float(curr["Close"]) > float(curr["Open"])
    engulfs = float(curr["Open"]) <= float(prev["Close"]) and float(curr["Close"]) >= float(prev["Open"])
    return prev_bear and curr_bull and engulfs


def _vol_ratio(df) -> float:
    """어제 거래량 / 최근 20일 평균 거래량."""
    if len(df) < 5:
        return 1.0
    avg = float(df["Volume"].iloc[:-1].mean())
    return float(df["Volume"].iloc[-1]) / avg if avg > 0 else 1.0


def _ma10_info(df) -> tuple[bool, float]:
    """(10일선 위 여부, 10일선 대비 괴리율 %)."""
    if len(df) < 10:
        return False, 0.0
    ma10 = float(df["Close"].rolling(10).mean().iloc[-1])
    close = float(df["Close"].iloc[-1])
    return close > ma10, round((close - ma10) / ma10 * 100, 2)


def _screen_one(ticker: str, name: str) -> Optional[dict]:
    import pandas as pd  # noqa: F401 — FDR 의존

    df = _fetch_sync(ticker, days=40)
    if df is None or len(df) < 12:
        return None

    above_ma, ma_dist = _ma10_info(df)
    vol = _vol_ratio(df)
    hammer = _hammer(df)
    mstar = _morning_star(df)
    engulf = _engulfing(df)

    score = 0
    patterns: list[str] = []

    # 10일선 위 + 과도하게 오르지 않은 구간 (매집 가능성)
    if above_ma and -2 <= ma_dist <= 8:
        score += 30

    # 거래량 급증 → 세력 매집 신호
    if vol >= 2.5:
        score += 45
        patterns.append(f"거래량 {vol:.1f}배 급증")
    elif vol >= 1.7:
        score += 25
        patterns.append(f"거래량 {vol:.1f}배 증가")

    # 캔들 패턴
    if hammer:
        score += 25
        patterns.append("망치형")
    if mstar:
        score += 30
        patterns.append("샛별형")
    if engulf:
        score += 25
        patterns.append("상승장악형")

    if score < 25:
        return None

    close = float(df["Close"].iloc[-1])
    prev_close = float(df["Close"].iloc[-2])
    change_pct = round((close - prev_close) / prev_close * 100, 2)
    five_day = (
        round((close - float(df["Close"].iloc[-6])) / float(df["Close"].iloc[-6]) * 100, 2)
        if len(df) >= 6 else 0.0
    )

    return {
        "ticker": ticker,
        "name": name,
        "score": score,
        "close": int(close),
        "change_pct": change_pct,
        "volume_ratio": round(vol, 2),
        "above_ma10": above_ma,
        "ma10_distance_pct": ma_dist,
        "five_day_return_pct": five_day,
        "patterns": patterns,
    }


async def screen_kospi_kosdaq(max_workers: int = 8, top_n: int = 8) -> list[dict]:
    """WATCHLIST 전체 스크리닝 → 고득점 순 top_n 반환."""
    sem = asyncio.Semaphore(max_workers)

    async def _run(ticker: str, name: str) -> Optional[dict]:
        async with sem:
            return await asyncio.to_thread(_screen_one, ticker, name)

    results = await asyncio.gather(*[_run(t, n) for t, n in WATCHLIST], return_exceptions=True)
    candidates = [r for r in results if isinstance(r, dict)]
    candidates.sort(key=lambda x: x["score"], reverse=True)
    logger.info("Screening done — %d/%d passed filters", len(candidates), len(WATCHLIST))
    return candidates[:top_n]
