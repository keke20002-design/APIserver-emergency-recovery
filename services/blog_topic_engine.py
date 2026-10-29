"""
Topic Engine — plan.md 기반 동적 주제 선정.
고정 키워드 풀(_KR_GUIDE_KEYWORDS, _EN_GUIDE_KEYWORDS) 대체.
Topic + Intent 조합으로 포스팅 키워드를 생성한다.
"""
import asyncio
import json
import logging
import os
import re
import random
import time
from datetime import datetime
from pathlib import Path

import httpx
from google import genai
from google.genai import types

from services.naver_api import search_news
from utils.cache import cache

logger = logging.getLogger(__name__)

# ── 반복 방지 설정값 (환경변수로 오버라이드 가능) ─────────────────────────────
# 최근 이력 조회 범위 (동일 파이프라인 기준)
TOPIC_RECENT_HISTORY_DAYS  = int(os.getenv("TOPIC_RECENT_HISTORY_DAYS", "30"))
TOPIC_RECENT_HISTORY_LIMIT = int(os.getenv("TOPIC_RECENT_HISTORY_LIMIT", "100"))
# 후보 키워드 대 과거 키워드 Jaccard 유사도 임계값 — 후보가 보통 "토픽+인텐트 2단어"의
# 3단어 구문이라 "같은 토픽 다른 인텐트"와 "다른 토픽 같은 인텐트"가 둘 다 Jaccard 0.5로
# 동일하게 나와 임계값으로 구분이 불가능하다. 0.75+로 두어 사실상 동일 문구만 걸러내는
# 안전망으로만 쓰고, 실제 반복 방지는 _score()의 날짜기반 패널티가 담당한다.
TOPIC_DUPLICATE_THRESHOLD  = float(os.getenv("TOPIC_DUPLICATE_THRESHOLD", "0.75"))
# 점수 단계 반복 패널티 — 값(감점) + 윈도우(일)
TOPIC_PENALTY         = float(os.getenv("TOPIC_PENALTY", "30"))
TOPIC_PENALTY_DAYS    = int(os.getenv("TOPIC_PENALTY_DAYS", "7"))
ENTITY_PENALTY        = float(os.getenv("TOPIC_ENTITY_PENALTY", "15"))
ENTITY_PENALTY_DAYS   = int(os.getenv("TOPIC_ENTITY_PENALTY_DAYS", "14"))
CATEGORY_PENALTY      = float(os.getenv("TOPIC_CATEGORY_PENALTY", "8"))
CATEGORY_PENALTY_DAYS = int(os.getenv("TOPIC_CATEGORY_PENALTY_DAYS", "30"))

# ── Topic 영역 (plan.md §Topic Pool) ─────────────────────────────────────────

TOPIC_AREAS: dict[str, dict] = {
    "AI": {
        "weight": 0.18,
        "topics": [
            "ChatGPT", "Claude", "Gemini", "Cursor", "Perplexity",
            "Copilot", "Midjourney", "NotebookLM", "Grok", "Windsurf", "Lovable",
        ],
    },
    "Automation": {
        "weight": 0.13,
        "topics": [
            "Claude Code", "MCP", "AI Agent", "n8n", "LangChain",
            "RAG", "Docker", "Python Automation", "Zapier", "Make",
        ],
    },
    "FullGuide": {
        "weight": 0.15,
        "topics": [
            "Claude Complete Guide", "ChatGPT Complete Guide",
            "AI Tools Comparison", "Blog Monetization Guide",
            "Google AdSense Guide", "n8n Automation Guide",
            "Claude Code Guide", "Gemini Complete Guide",
            "Bitcoin Complete Guide", "ETF Investing Guide",
        ],
    },
    "Guides": {
        "weight": 0.12,
        "topics": [
            "AdSense", "Blog Monetization", "Solo Development", "SaaS", "AI Side Hustle",
            "Productivity", "Notion", "Freelancing", "Startup",
        ],
    },
    "Market": {
        "weight": 0.10,
        "topics": [
            "NVIDIA", "Bitcoin", "ETF", "AI Stocks", "Semiconductor",
            "US Stocks", "TSLA", "Data Center",
        ],
    },
    "Consumer Tech": {
        "weight": 0.05,
        "topics": ["iPhone", "Galaxy", "MacBook", "GPU", "Apple", "Samsung"],
    },
    "Trending": {
        "weight": 0.05,
        "topics": [],  # 런타임에 외부 소스로 채워짐
    },
    "Career": {
        "weight": 0.12,
        "topics": [
            "Job Change Strategy", "Salary Negotiation", "AI Era Jobs", "Remote Work",
            "Side Project", "Portfolio Building", "Solo Entrepreneurship",
            "Personal Branding", "LinkedIn Strategy", "Career Change",
        ],
    },
    "Creator": {
        "weight": 0.10,
        "topics": [
            "YouTube Algorithm", "Newsletter Monetization", "Blog SEO",
            "Short-Form Content", "Content Strategy", "Digital Nomad",
            "Online Course Creation", "eBook Publishing", "Community Management",
        ],
    },
    "Programming": {
        "weight": 0.05,
        "topics": [
            "Python", "JavaScript", "TypeScript", "React", "FastAPI",
            "Docker", "Git", "REST API", "SQL", "Linux Commands",
        ],
    },
    "Tools": {
        "weight": 0.04,
        "topics": [
            "Notion", "Figma", "Obsidian", "VS Code", "Raycast",
            "Arc Browser", "Zed Editor", "Linear", "Supabase", "Vercel",
        ],
    },
    "Crypto": {
        "weight": 0.04,
        "topics": [
            "Bitcoin", "Ethereum", "Ripple", "Solana", "DeFi",
            "Crypto Investing", "NFT", "Web3", "Altcoin",
        ],
    },
}

# ── EN Hub-and-Spoke 구조 (WordPress EN 전용, KR/Naver 미사용) ────────────────
# group: WordPress 카테고리 taxonomy로 그대로 publish_post(category=)에 전달됨
#        (WP는 terms_names.category에 없는 이름을 주면 자동 생성하므로 새 그룹명 안전)
EN_HUBS: dict[str, dict] = {
    # AI — 채팅/웹 제품, "설치"가 성립하지 않음
    "ChatGPT":     {"group": "AI", "searchability": 95, "vs": ["Claude", "Gemini"], "installable": False},
    "Claude":      {"group": "AI", "searchability": 90, "vs": ["ChatGPT", "Gemini"], "installable": False},
    "Gemini":      {"group": "AI", "searchability": 90, "vs": ["ChatGPT", "Claude"], "installable": False},
    "Grok":        {"group": "AI", "searchability": 62, "vs": ["ChatGPT", "Claude"], "installable": False},
    "Perplexity":  {"group": "AI", "searchability": 70, "vs": ["ChatGPT", "Google Search"], "installable": False},
    # Developer — 로컬 설치/확장 프로그램
    "Cursor":          {"group": "Developer", "searchability": 82, "vs": ["Claude Code", "GitHub Copilot"], "installable": True},
    "Claude Code":     {"group": "Developer", "searchability": 85, "vs": ["Cursor", "GitHub Copilot"], "installable": True},
    "MCP":             {"group": "Developer", "searchability": 75, "vs": ["LangChain"], "installable": True},
    "Docker":          {"group": "Developer", "searchability": 65, "vs": ["Podman"], "installable": True},
    "VSCode":          {"group": "Developer", "searchability": 68, "vs": ["Cursor", "Zed"], "installable": True},
    "GitHub Copilot":  {"group": "Developer", "searchability": 70, "vs": ["Cursor", "Claude Code"], "installable": True},
    # Automation — n8n은 셀프호스팅 설치 가능, 나머지는 가입형 SaaS
    "n8n":         {"group": "Automation", "searchability": 65, "vs": ["Zapier", "Make"], "installable": True},
    "Zapier":      {"group": "Automation", "searchability": 60, "vs": ["n8n", "Make"], "installable": False},
    "Make":        {"group": "Automation", "searchability": 55, "vs": ["n8n", "Zapier"], "installable": False},
    "OpenRouter":  {"group": "Automation", "searchability": 48, "vs": ["OpenAI API"], "installable": False},
    # Business — 개념/전략 허브, 설치 대상 아님
    "AI Marketing":    {"group": "Business", "searchability": 55, "vs": [], "installable": False},
    "AI SEO":          {"group": "Business", "searchability": 55, "vs": [], "installable": False},
    "AI Ads":          {"group": "Business", "searchability": 46, "vs": [], "installable": False},
    "AI Productivity": {"group": "Business", "searchability": 57, "vs": [], "installable": False},
    # Investment — 종목/기업, 설치 대상 아님
    "Nvidia":  {"group": "Investment", "searchability": 90, "vs": ["AMD"], "installable": False},
    "AI ETF":  {"group": "Investment", "searchability": 78, "vs": ["individual AI stocks"], "installable": False},
    "OpenAI":  {"group": "Investment", "searchability": 80, "vs": ["Anthropic", "Google DeepMind"], "installable": False},
    "AMD":     {"group": "Investment", "searchability": 70, "vs": ["Nvidia"], "installable": False},

    # ── Software (콘텐츠 전략 확장: AI만이 아닌 "Digital Tech & Productivity") ──
    "Chrome":           {"group": "Software", "ratio_bucket": "Software", "searchability": 85, "vs": ["Edge", "Firefox"], "installable": True},
    "Edge":             {"group": "Software", "ratio_bucket": "Software", "searchability": 60, "vs": ["Chrome"], "installable": True},
    "Discord":          {"group": "Apps", "ratio_bucket": "Software", "searchability": 78, "vs": ["Slack", "Telegram"], "installable": True},
    "Zoom":             {"group": "Software", "ratio_bucket": "Software", "searchability": 65, "vs": ["Google Meet", "Teams"], "installable": True},
    "Microsoft Copilot":{"group": "Software", "ratio_bucket": "Software", "searchability": 62, "vs": ["ChatGPT"], "installable": False},
    "OBS Studio":       {"group": "Software", "ratio_bucket": "Software", "searchability": 58, "vs": ["Streamlabs"], "installable": True},
    "GitHub Desktop":   {"group": "Software", "ratio_bucket": "Software", "searchability": 45, "vs": ["GitHub CLI"], "installable": True},
    "WhatsApp":         {"group": "Apps", "ratio_bucket": "Software", "searchability": 80, "vs": ["Telegram", "Signal"], "installable": True},
    "Telegram":         {"group": "Apps", "ratio_bucket": "Software", "searchability": 65, "vs": ["WhatsApp", "Signal"], "installable": True},
    "Signal":           {"group": "Apps", "ratio_bucket": "Software", "searchability": 45, "vs": ["WhatsApp", "Telegram"], "installable": True},
    "Spotify":          {"group": "Apps", "ratio_bucket": "Software", "searchability": 75, "vs": ["Apple Music"], "installable": True},
    "Netflix":          {"group": "Apps", "ratio_bucket": "Software", "searchability": 70, "vs": ["Disney+"], "installable": False},
    "YouTube":          {"group": "Apps", "ratio_bucket": "Software", "searchability": 85, "vs": [], "installable": False},

    # ── Productivity ──
    "Notion":           {"group": "Productivity", "ratio_bucket": "Productivity", "searchability": 68, "vs": ["Obsidian", "Evernote"], "installable": True},
    "Canva":            {"group": "Productivity", "ratio_bucket": "Productivity", "searchability": 72, "vs": ["Adobe Express"], "installable": False},
    "Adobe Express":    {"group": "Productivity", "ratio_bucket": "Productivity", "searchability": 42, "vs": ["Canva"], "installable": False},
    "Microsoft Office": {"group": "Productivity", "ratio_bucket": "Productivity", "searchability": 70, "vs": ["Google Docs"], "installable": True},
    "Google Drive":     {"group": "Productivity", "ratio_bucket": "Productivity", "searchability": 66, "vs": ["Dropbox", "OneDrive"], "installable": False},
    "Dropbox":          {"group": "Productivity", "ratio_bucket": "Productivity", "searchability": 55, "vs": ["Google Drive"], "installable": True},
    "CapCut":           {"group": "Productivity", "ratio_bucket": "Productivity", "searchability": 68, "vs": ["Premiere Pro"], "installable": True},

    # ── Windows / Mac ──
    "Windows 11":       {"group": "Windows", "ratio_bucket": "Windows", "searchability": 82, "vs": ["Windows 12", "macOS"], "installable": True},
    "Windows 12":       {"group": "Windows", "ratio_bucket": "Windows", "searchability": 40, "vs": ["Windows 11"], "installable": True},
    "File Explorer":    {"group": "Windows", "ratio_bucket": "Windows", "searchability": 50, "vs": [], "installable": False},
    "PowerShell":       {"group": "Windows", "ratio_bucket": "Windows", "searchability": 58, "vs": ["Command Prompt"], "installable": False},
    "Task Manager":     {"group": "Windows", "ratio_bucket": "Windows", "searchability": 48, "vs": [], "installable": False},
    "Windows Copilot":  {"group": "Windows", "ratio_bucket": "Windows", "searchability": 45, "vs": [], "installable": False},
    "PowerToys":        {"group": "Windows", "ratio_bucket": "Windows", "searchability": 47, "vs": [], "installable": True},
    "macOS":            {"group": "Mac", "ratio_bucket": "Windows", "searchability": 60, "vs": ["Windows 11"], "installable": False},

    # ── Android / iPhone ──
    "Samsung":          {"group": "Android", "ratio_bucket": "Android", "searchability": 75, "vs": ["Pixel", "iPhone"], "installable": False},
    "Pixel":            {"group": "Android", "ratio_bucket": "Android", "searchability": 55, "vs": ["Samsung", "iPhone"], "installable": False},
    "Android 16":       {"group": "Android", "ratio_bucket": "Android", "searchability": 50, "vs": ["iOS"], "installable": False},
    "iOS":              {"group": "iPhone", "ratio_bucket": "Android", "searchability": 70, "vs": ["Android 16"], "installable": False},
    "Safari":           {"group": "iPhone", "ratio_bucket": "Android", "searchability": 52, "vs": ["Chrome"], "installable": False},
    "Shortcuts":        {"group": "iPhone", "ratio_bucket": "Android", "searchability": 46, "vs": [], "installable": False},
}

# 기존 23개 허브(AI/Developer/Automation/Business/Investment)는 ratio_bucket 명시 안 함 → 전부 "AI" 버킷
for _meta in EN_HUBS.values():
    _meta.setdefault("ratio_bucket", "AI")

# 검색량 하드 컷오프 — 미만이면 pick_topic_en_hub()에서 해당 허브 전체 스킵
EN_HUB_MIN_SEARCHABILITY = int(os.getenv("EN_HUB_MIN_SEARCHABILITY", "40"))
EN_EVERGREEN_RATIO = float(os.getenv("EN_EVERGREEN_RATIO", "0.70"))  # 70% evergreen / 30% trending

# "Digital Tech & Productivity" 콘텐츠 비율 — AI만 생성하지 않도록 버킷별 가중치.
# 제목 타입(How-To/Comparison/Best/... _TITLE_TYPES) 비율이 Review/Comparison 비중을 별도로 담당.
EN_RATIO_WEIGHTS: dict[str, float] = {
    "AI": 0.30, "Software": 0.25, "Windows": 0.15, "Android": 0.10, "Productivity": 0.10,
}
_TRAFFIC_SCORE_MIN = float(os.getenv("EN_TRAFFIC_SCORE_MIN", "70"))


def _slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


# ── 검색 의도 기반 제목 생성 (템플릿 리스트 대체) ─────────────────────────────

_TITLE_INTENTS = [
    "Beginner", "Tutorial", "Comparison", "Pricing", "Troubleshooting", "Review",
    "Alternatives", "News", "API", "Best Practices", "Tips", "Examples",
]

# 후보 title_seed에서 어떤 패턴을 썼는지 판별할 때도 재사용 (구체적인/긴 패턴 우선 매칭).
# "Troubleshooting"/"Workflow"는 의도적으로 제외 — 실제 검색 의도를 담은 자연스러운 문구
# (Not Working / Keeps Crashing / Worth It 등) 대신 라벨 단어를 그대로 제목 앞에 붙이는
# 게으른 패턴을 코드 레벨에서 "허용된 패턴"으로 인정해주지 않기 위함.
_EN_ALLOWED_TITLE_PATTERNS = [
    # 문제/증상 — 라벨이 아니라 실제 증상을 문장으로
    "Not Working", "Keeps Crashing", "Won't Open", "Won't Start", "How to Fix",
    # 비교
    "vs", "Which Is Better", "Comparison",
    # 구매/선택 판단
    "Worth It", "Worth Paying", "Should You Pay", "Should You Switch",
    # 목록
    "Best", "Top",
    # 정보
    "Pricing", "Alternatives", "Features",
    "Pros & Cons", "Case Study", "Best Practices", "Checklist", "Tutorial",
    "Examples", "Mistakes", "Review", "Prompt", "What is", "How to", "API", "Why",
]
_EN_BANNED_TITLE_WORDS = [
    "Guide", "Complete Guide", "Ultimate Guide", "Practical Guide", "Master Guide", "Walkthrough",
    "Ultimate", "Verdict", "Who Benefits", "Your Workflow", "Everything You Need to Know",
    "Everything About", "The Future of", "A New Era of", "New Era of",
]

# 완전 금지 — 너무 흔하고 경쟁이 심한 문구는 부분 문자열로도 하드 차단 (코드 레벨 안전망)
_EN_BANNED_TITLE_PHRASES = ["how to install", "how to use", "how to start"]

# ── 제목 타입 비율 (제목 다양성 최우선) ──────────────────────────────────────
_TITLE_TYPES: dict[str, float] = {
    "Troubleshooting": 0.20, "Comparison": 0.20, "Best": 0.15, "Review": 0.15,
    "Alternatives": 0.10, "How-To": 0.10, "Pricing": 0.05, "News Analysis": 0.05,
}
# 기존 12개 검색의도(_TITLE_INTENTS)를 8개 제목 타입으로 매핑
_INTENT_TO_TITLE_TYPE: dict[str, str] = {
    "Beginner": "How-To", "Tutorial": "How-To", "API": "How-To",
    "Comparison": "Comparison",
    "Best Practices": "Best", "Tips": "Best", "Examples": "Best",
    "Troubleshooting": "Troubleshooting",
    "Review": "Review",
    "Alternatives": "Alternatives",
    "Pricing": "Pricing",
    "News": "News Analysis",
}

_TITLE_TYPE_STYLE_GUIDE: dict[str, str] = {
    "How-To": (
        "Prefer PROBLEM-FORMAT over a flat imperative — lead with the exact situation, then the fix. "
        "NEVER produce a bare 'How to Install X' / 'How to Use X' / 'How to Start X' (banned, too generic/competitive). "
        "Good: 'Chrome Won't Install on Windows? Here's How to Fix It' / 'Notion Desktop Not Opening? Try These Solutions'."
    ),
    "Comparison": (
        "MUST include a stated purpose or audience — bare 'X vs Y' is banned. "
        "Good: 'Claude vs ChatGPT for Coding' / 'Cursor vs GitHub Copilot for Developers' / "
        "'Perplexity vs Google Search: Which Is Better for Research?' / 'Gemini vs ChatGPT for Students'."
    ),
    "Best": (
        "Listicle framing with a clear audience or year. Prefer 'Best' over 'Top N' as the default — "
        "only use 'Top N' when a specific ranked count genuinely adds value, and never two 'Top N' "
        "titles in a row. "
        "Good: 'Best Free AI Tools for Students' / 'Best Chrome Extensions for Productivity' / "
        "'Best PDF Editors in 2026' / 'Best Note Taking Apps'."
    ),
    "Troubleshooting": (
        "NEVER start the title with the label word 'Troubleshooting' (e.g. 'Troubleshooting File "
        "Explorer Crashing' is banned — it reads like a support-ticket category, not something a "
        "person types into Google). State the exact symptom as the reader would search it, as a "
        "question, then the fix count. "
        "Good: 'File Explorer Keeps Crashing? 7 Ways to Fix It on Windows 11' / "
        "'Discord Keeps Crashing? 8 Fixes That Actually Work' / "
        "'Android 16 Camera Not Working? Try These 8 Fixes' / "
        "'Windows 11 Running Slow? Here's What to Do'."
    ),
    "Review": (
        "Opinionated, first-person-adjacent framing built around a real decision the reader is "
        "trying to make — never use the bare word 'Verdict' as a title tag. "
        "Good: 'Is Notion AI Worth It?' / 'Notion AI Review: Is It Worth Paying for in 2026?' / "
        "'Cursor AI Review After 30 Days' / 'Canva Pro Review'."
    ),
    "Alternatives": (
        "Good: 'Best ChatGPT Alternatives' / 'Top Notion Alternatives' / 'Google Drive Alternatives'."
    ),
    "Pricing": (
        "Good: 'ChatGPT Pricing Explained' / 'Claude Pro vs Max Pricing' / 'Gemini API Cost Guide'."
    ),
    "News Analysis": (
        "NEVER just summarize the news, and never use the bare phrase 'Who Benefits?' — it's been "
        "overused into a cliché. State a specific, varied analysis angle instead — a real question "
        "tied to the actual news, not a generic template phrase reused across posts. "
        "Good: 'OpenAI Released GPT-5: What It Means for Developers' / "
        "'GitHub Copilot Changes Pricing: Should You Switch?' / "
        "'Claude's New Rate Limits: Is This Actually a Problem for You?' / "
        "'Gemini 3 Launched: What Changes for Existing Users'."
    ),
}


def _title_pattern_of(title: str) -> str | None:
    """제목에 쓰인 허용 패턴 판별 (가장 구체적인/긴 패턴 우선 매칭)."""
    t = title.lower()
    for p in _EN_ALLOWED_TITLE_PATTERNS:
        if p.lower() in t:
            return p
    return None


def _recent_title_patterns(titles: list[str], limit: int = 2) -> list[str]:
    return [p for t in titles[:limit] if (p := _title_pattern_of(t))]


def _traffic_score(candidate: dict, meta: dict, sibling_count: int) -> float:
    """0~100 결정론적 점수 — Search Intent 명확성 + Evergreen + CTR예상 + Topic Authority + 경쟁도.
    별도 Gemini 호출 없이 이미 갖고 있는 신호만 재사용(파이프라인당 Gemini 호출 수가 이미 많아 비용 관리 목적)."""
    title = candidate["title_seed"]

    # 검색의도 명확성 — 후보가 허용 패턴을 실제로 쓰고 있는지 (구조상 거의 항상 True)
    intent_fit = 100.0 if _title_pattern_of(title) else 50.0

    # Evergreen 여부 — News 인텐트(트렌딩)만 낮게, 나머지는 evergreen으로 간주
    evergreen = 100.0 if candidate.get("intent") != "News" else 40.0

    # CTR 예상 — 물음표/숫자 포함, 적정 길이
    ctr = 55.0
    if "?" in title:
        ctr += 15
    if re.search(r"\d", title):
        ctr += 10
    if len(title) <= 60:
        ctr += 15
    ctr = min(ctr, 100.0)

    # Topic Authority — 이 허브에 이미 쌓인 형제글 수 (시리즈 효과, 과포화는 완만히 감점)
    if sibling_count == 0:
        authority = 50.0
    elif sibling_count <= 3:
        authority = 75.0
    elif sibling_count <= 8:
        authority = 90.0
    else:
        authority = 70.0

    # 경쟁도 — searchability가 중간대(약 65)일 때 최적, 너무 낮거나(무명) 너무 높으면(초대형 키워드) 감점
    competition = max(0.0, 100.0 - abs(meta["searchability"] - 65) * 1.2)

    return intent_fit * 0.25 + evergreen * 0.20 + ctr * 0.20 + authority * 0.20 + competition * 0.15


_CTR_SCORE_MIN = float(os.getenv("EN_CTR_SCORE_MIN", "80"))


def _ctr_score(title_seed: str, title_type: str, recent_titles: list[str]) -> float:
    """0~100 결정론적 CTR 점수 — 검색의도/명확성/클릭유도/길이(50~65자)/중복도.
    별도 Gemini 호출 없이 제목 문자열만으로 계산 (비용 관리 목적, _traffic_score와 동일 철학)."""
    t = title_seed
    score = 40.0

    n = len(t)
    if 50 <= n <= 65:
        score += 20
    elif 40 <= n < 50 or 65 < n <= 75:
        score += 8

    if _title_pattern_of(t):
        score += 15

    # 타입별 필수 요소 — 없으면 클릭 유도력이 떨어진다고 보고 감점
    tl = t.lower()
    if title_type == "Comparison" and (" for " not in tl and ":" not in t):
        score -= 25  # 목적/대상 없는 bare "X vs Y"
    if title_type == "News Analysis" and not any(
        p in tl for p in ("what it means", "should you", "is this", "what changes")
    ):
        score -= 25
    if title_type == "Troubleshooting" and not re.search(r"\d+\s+(fix|fixes|ways|tips|solutions)", tl):
        score -= 10  # 구체적 개수 없으면 약한 감점(필수는 아님)

    if "?" in t or re.search(r"\d", t):
        score += 15

    if _too_similar(t, recent_titles[:20]):
        score -= 30  # 중복도

    return max(0.0, min(100.0, score))


def _violates_banned_phrase(title_seed: str) -> bool:
    t = title_seed.lower()
    return any(p in t for p in _EN_BANNED_TITLE_PHRASES) or any(w.lower() in t for w in _EN_BANNED_TITLE_WORDS)


async def _generate_title_candidates_en(hub: str, meta: dict, existing_keys: set[str], target_type: str) -> list[dict]:
    """허브 하나당 Gemini 1회 호출로 ≥15개 제목 후보 생성 (베스트 먼저 랭크), target_type 위주로 유도.
    고정 템플릿을 모든 허브에 기계적으로 적용하던 방식(예: 채팅 제품에도 'How to
    Install' 적용) 대신, 허브의 설치 가능 여부·검색 의도를 반영해 매번 새로 생성한다.
    Returns: [{key, title_seed, intent, pattern, title_type}]"""
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        return []
    install_rule = (
        "This IS a locally installed tool — 'Install' angles are allowed (but still avoid the flat "
        "banned phrasing below — use problem-format instead)."
        if meta["installable"] else
        "This is NOT something you install (web app / API / SaaS / investment) — "
        "NEVER use the word 'Install' in any candidate. Use 'How to Use', 'Getting Started', "
        "'First Steps' instead."
    )
    prompt = (
        f'You run a topical-authority tech blog hub about "{hub}" ({meta["group"]} category), '
        f"aiming to look like a real SEO tech media, not a tutorial dump. Title diversity is the "
        f"top priority — readers should think \"I was wondering about this\" from the title alone.\n"
        f"{install_rule}\n"
        f"Already published angles for this hub: {sorted(existing_keys) or 'none'}.\n\n"
        f"Write each title_seed as a natural sentence a real person would type into Google, not a "
        f"category label with a topic bolted on.\n"
        f"  Bad: 'Troubleshooting File Explorer Crashing' -> "
        f"Good: 'File Explorer Keeps Crashing? 7 Ways to Fix It on Windows 11'\n"
        f"  Bad: '{hub} Review: AI Productivity Verdict' -> "
        f"Good: '{hub} Review: Is It Worth Paying for in 2026?'\n"
        f"After drafting each candidate, check: \"if someone clicked this, would they know exactly "
        f"what they're about to learn?\" If the answer is unclear, rewrite it — vague titles like "
        f"'Who Benefits?' or 'Your Workflow' fail this check and must not be used.\n\n"
        f"Generate at least 15 NEW candidate blog post angles for this hub, ranked BEST first "
        f"(most likely to earn real Google clicks first). At least 10 of the 15 candidates must be "
        f"the '{target_type}' title type: {_TITLE_TYPE_STYLE_GUIDE[target_type]}\n"
        f"The remaining candidates can be other types from: {', '.join(_TITLE_TYPES)}\n\n"
        f"Each candidate must map to exactly one search intent from: {', '.join(_TITLE_INTENTS)}\n"
        f"Each candidate's title_seed must clearly use exactly one of these patterns: "
        f"{', '.join(_EN_ALLOWED_TITLE_PATTERNS)}\n"
        f"NEVER use these words/phrases anywhere in a title: {', '.join(_EN_BANNED_TITLE_WORDS)}\n"
        f"NEVER produce these exact generic phrasings (too competitive/overused): "
        f"{', '.join(_EN_BANNED_TITLE_PHRASES)}\n"
        f"Vary the pattern across candidates — do not repeat the same pattern more than twice in a row.\n"
        f"title_seed should be a short angle phrase (3-10 words), not a full polished headline — "
        f"the article writer will polish it later, but it should already read like a real clickable title.\n\n"
        'Output JSON only: [{"key": "short_snake_case_id", "title_seed": "...", '
        '"intent": "one of the intents above", "pattern": "one of the patterns above"}]'
    )
    try:
        client = genai.Client(api_key=api_key)
        response = await client.aio.models.generate_content(
            model=_GEMINI_MODEL, contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.7, max_output_tokens=3072,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", (response.text or "").strip())
        parsed = json.loads(raw)
        out = []
        for d in parsed if isinstance(parsed, list) else []:
            key = f"gen-{_slugify(d.get('key', ''))[:24]}"
            title_seed = d.get("title_seed", "")
            if key in existing_keys or not title_seed:
                continue
            if not meta["installable"] and "install" in title_seed.lower():
                continue  # 코드 레벨 안전망 — 프롬프트가 놓쳐도 걸러냄
            if _violates_banned_phrase(title_seed):
                continue  # 하드 금지 문구(how to install/use/start, Guide류) — 프롬프트가 놓쳐도 걸러냄
            intent = d.get("intent") if d.get("intent") in _TITLE_INTENTS else "Tips"
            out.append({
                "key": key, "title_seed": title_seed, "intent": intent,
                "pattern": d.get("pattern") or _title_pattern_of(title_seed) or "How to",
                "title_type": _INTENT_TO_TITLE_TYPE.get(intent, "How-To"),
            })
        return out
    except Exception as e:
        logger.warning("Title candidate generation failed for hub=%s: %s", hub, e)
        return []


_EN_HUB_NAMES = list(EN_HUBS.keys())


async def _fetch_en_hub_trending() -> list[dict]:
    """TrendLab Global(GitHub Trending/Product Hunt/HN/Reddit/News/YouTube 가중 통합) →
    EN 허브 중 매칭되는 것만 추출. 2시간 캐시(빈 결과는 30분)."""
    cache_key = "en_hub_trending_v2"  # v1(HN+Reddit only) → v2(TrendLab Global) — 소스가 바뀌어 캐시 키 새로
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    from app.trendlab_global.providers import collect_all_weighted
    weighted_items = await collect_all_weighted()
    if not weighted_items:
        cache.set(cache_key, [], ttl=1800)
        return []
    all_titles = [it.title for it in weighted_items[:60]]  # 이미 가중치 반영해 정렬된 상위 60개만
    items = await _extract_en_hub_trending_gemini(all_titles)
    cache.set(cache_key, items, ttl=7200)
    return items


async def _extract_en_hub_trending_gemini(headlines: list[str]) -> list[dict]:
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key or not headlines:
        return []
    sample = headlines[:40]
    hub_list = ", ".join(_EN_HUB_NAMES)
    text = "\n".join(f"- {h}" for h in sample)
    prompt = (
        f"You track a tech blog covering ONLY these named tools/companies: {hub_list}\n\n"
        "From the headlines below, pick ones reporting real NEWS (release, price change, "
        "outage, benchmark, funding, controversy) about ONE of these exact names — not a "
        "passing mention. Max 6 matches.\n"
        "Do NOT just summarize the news. title_seed MUST include a specific analysis angle tied to "
        "the actual story (e.g. what it means for a named audience, whether it's worth switching "
        "over, what changes for existing users) — not a flat headline restatement, and never the "
        "generic overused phrase 'Who Benefits?'.\n"
        f"Title must clearly use exactly one of these patterns: {', '.join(_EN_ALLOWED_TITLE_PATTERNS)}. "
        f"NEVER use: {', '.join(_EN_BANNED_TITLE_WORDS)}.\n"
        f"Map each to exactly one intent: {', '.join(_TITLE_INTENTS)}.\n"
        "Return ONLY JSON, no explanation.\n\n"
        'Example: [{"hub": "Claude", "title_seed": "Claude 4.5 Release: What It Means for Developers", "intent": "News"}]\n\n'
        f"Headlines:\n{text}"
    )
    try:
        client = genai.Client(api_key=api_key)
        response = await client.aio.models.generate_content(
            model=_GEMINI_MODEL, contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.3, max_output_tokens=2048,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", (response.text or "").strip())
        parsed = json.loads(raw)
        out = []
        today = datetime.now().strftime("%Y%m%d")
        for it in parsed if isinstance(parsed, list) else []:
            hub = it.get("hub", "")
            if hub not in EN_HUBS:
                continue
            title_seed = it.get("title_seed", "")[:120]
            if not EN_HUBS[hub]["installable"] and "install" in title_seed.lower():
                continue  # 코드 레벨 안전망 — 설치 대상 아닌 허브에 Install 문구 방지
            out.append({
                "hub": hub, "title_seed": title_seed,
                "intent": it.get("intent") if it.get("intent") in _TITLE_INTENTS else "News",
                "subtopic_key": f"trend-{_slugify(title_seed)[:24]}-{today}",
            })
        return out
    except Exception as e:
        logger.warning("EN hub trending extract failed: %s", e)
        return []


async def _pick_trending_hub_item(exclude_keywords: list[str], recent_titles: list[str] | None = None) -> dict | None:
    from services.blog_db import get_hub_post_counts
    items = await _fetch_en_hub_trending()
    random.shuffle(items)
    counts = None
    recent_titles = recent_titles or []
    for it in items:
        if _too_similar(it["title_seed"], exclude_keywords):
            continue
        meta = EN_HUBS[it["hub"]]
        if counts is None:
            counts = get_hub_post_counts(language="en")
        cand = {"title_seed": it["title_seed"], "intent": it["intent"]}
        score = _traffic_score(cand, meta, counts.get(it["hub"], 0))
        if score < _TRAFFIC_SCORE_MIN:
            continue  # Traffic Score 게이트 — 트렌딩도 예외 없음
        ctr = _ctr_score(it["title_seed"], "News Analysis", recent_titles)
        if ctr < _CTR_SCORE_MIN:
            continue  # CTR Score 게이트 — "What It Means" 류 분석 앵글 없으면 여기서 걸러짐
        return {"keyword": it["title_seed"], "category": meta["group"],
                "hub": it["hub"], "subtopic_key": it["subtopic_key"], "intent": it["intent"], "is_evergreen": False}
    return None


async def pick_topic_en_hub(exclude_keywords: list[str] | None = None, recent_titles: list[str] | None = None) -> dict:
    """
    EN hub-and-spoke 토픽 선정. pick_topic()과 완전 독립 — TOPIC_AREAS/기존 KR 경로 미사용.
    서브토픽은 고정 템플릿이 아니라 허브별로 Gemini가 매번 새로 생성한 후보 중에서 고른다
    (설치 가능 여부·검색 의도를 반영, 사이트 전역 최근 제목과 패턴이 3연속 겹치지 않도록 필터링,
    Traffic Score 70점/CTR Score 80점 미만은 생성 후보에서 제외, 직전 글과 같은 콘텐츠 버킷/
    제목 타입은 연속 방지).
    Returns: {keyword, category, hub, subtopic_key, intent, is_evergreen}
    """
    from services.blog_db import (
        get_hub_used_subtopics, get_hub_post_counts, get_last_post_hub, get_last_post_intent,
    )
    exclude_keywords = exclude_keywords or []
    recent_titles = recent_titles or []

    # 0) 직전 글의 제목 타입 — 트렌딩(항상 News Analysis)/evergreen 양쪽에서 연속 방지 대상
    last_intent = get_last_post_intent(language="en")
    last_title_type = _INTENT_TO_TITLE_TYPE.get(last_intent) if last_intent else None

    # 1) 70/30 evergreen/trending 결정 (per-pick 독립 베르누이 시행)
    is_evergreen = random.random() < EN_EVERGREEN_RATIO
    if not is_evergreen and last_title_type != "News Analysis":
        trending = await _pick_trending_hub_item(exclude_keywords, recent_titles)
        if trending:
            return trending
        # 트렌딩 소재 없으면(또는 직전 글이 이미 News Analysis라 연속 방지 대상이면) evergreen으로 폴백

    counts = get_hub_post_counts(language="en")
    recent_patterns = _recent_title_patterns(recent_titles)  # 최근 2개 사이트 전역 EN 제목의 패턴

    # 2) 직전 글의 ratio_bucket은 이번 회차에서 제외 (카테고리 연속 방지)
    last_hub = get_last_post_hub(language="en")
    last_bucket = EN_HUBS.get(last_hub, {}).get("ratio_bucket") if last_hub else None
    remaining = dict(EN_RATIO_WEIGHTS)
    if last_bucket in remaining and len(remaining) > 1:
        remaining.pop(last_bucket)

    # 2.5) 제목 타입도 8종 비율로 목표 선정, 직전 글과 같은 타입은 연속 방지
    type_pool = dict(_TITLE_TYPES)
    if last_title_type in type_pool and len(type_pool) > 1:
        type_pool.pop(last_title_type)
    target_type = random.choices(list(type_pool.keys()), weights=list(type_pool.values()), k=1)[0]

    while remaining:
        bucket = random.choices(list(remaining.keys()), weights=list(remaining.values()), k=1)[0]
        hubs_in_bucket = sorted(
            (h for h, m in EN_HUBS.items()
             if m["ratio_bucket"] == bucket and m["searchability"] >= EN_HUB_MIN_SEARCHABILITY),
            key=lambda h: counts.get(h, 0),
        )

        for hub in hubs_in_bucket:
            meta = EN_HUBS[hub]
            used = get_hub_used_subtopics(hub)
            candidates = await _generate_title_candidates_en(hub, meta, used, target_type)
            if not candidates:
                continue  # 생성 실패 → 다음 허브로

            sibling_count = counts.get(hub, 0)
            valid = []
            for cand in candidates:
                ts = _traffic_score(cand, meta, sibling_count)
                if ts < _TRAFFIC_SCORE_MIN:
                    continue
                cs = _ctr_score(cand["title_seed"], cand["title_type"], recent_titles)
                if cs < _CTR_SCORE_MIN:
                    continue
                valid.append(cand)
            if not valid:
                continue  # 전부 Traffic/CTR Score 미달 → 다음 허브로

            # target_type과 일치하는 후보 우선 (Gemini가 이미 베스트 순으로 랭크한 순서 유지)
            type_matched = [cand for cand in valid if cand["title_type"] == target_type]
            search_pool = type_matched or valid

            chosen = None
            for cand in search_pool:
                if _too_similar(cand["title_seed"], exclude_keywords):
                    continue
                if len(recent_patterns) >= 2 and recent_patterns[0] == recent_patterns[1] == cand["pattern"]:
                    continue  # 3연속 동일 패턴 방지
                chosen = cand
                break
            if not chosen:
                continue  # 후보 전부 필터링됨 → 다음 허브로

            return {
                "keyword": chosen["title_seed"], "category": meta["group"], "hub": hub,
                "subtopic_key": chosen["key"], "intent": chosen["intent"], "is_evergreen": True,
            }

        remaining.pop(bucket, None)  # 이 버킷 소진 — 다음 버킷으로 재시도

    # 3) 전체 실패 — 하드 폴백
    logger.warning("EN hub engine: all buckets exhausted, fallback to Claude Code install")
    return {"keyword": "How to Install Claude Code", "category": "Developer",
            "hub": "Claude Code", "subtopic_key": "install", "intent": "Tutorial", "is_evergreen": True}


# ── Intent → 키워드 템플릿 (plan.md §Intent Engine) ──────────────────────────

_INTENT_KR: dict[str, str] = {
    "HowTo":        "{topic} 방법",
    "Tutorial":     "{topic} 튜토리얼",
    "Cost":         "{topic} 비용 정리",
    "Comparison":   "{topic} 비교",
    "Review":       "{topic} 써보니",
    "Productivity": "{topic} 생산성 활용",
    "Workflow":     "{topic} 워크플로우",
    "Automation":   "{topic} 자동화 방법",
    "CaseStudy":    "{topic} 활용 사례",
    "SideHustle":   "{topic} 수익화 방법",
    "FAQ":          "{topic} 정리",
    "Prediction":   "{topic} 전망",
    "FullGuide":    "{topic} 완전 정리 (2026 최신)",
    "Beginner":     "{topic} 입문부터 실전까지",
    "Ultimate":     "{topic} 모든 것 정리",
}

_INTENT_EN: dict[str, str] = {
    "HowTo":        "how to use {topic}",
    "Tutorial":     "{topic} tutorial guide",
    "Cost":         "{topic} pricing explained",
    "Comparison":   "{topic} vs alternatives",
    "Review":       "{topic} review",
    "Productivity": "{topic} productivity tips",
    "Workflow":     "{topic} workflow guide",
    "Automation":   "automate with {topic}",
    "CaseStudy":    "{topic} real-world use cases",
    "SideHustle":   "make money with {topic}",
    "FAQ":          "{topic} FAQ guide",
    "Prediction":   "{topic} future outlook",
    "FullGuide":    "complete {topic} guide (2026)",
    "Beginner":     "{topic} for beginners: full walkthrough",
    "Ultimate":     "everything you need to know about {topic}",
}

# 에버그린 인텐트 → 장기 가치 점수 높음
_EVERGREEN_INTENTS = {"HowTo", "Tutorial", "Cost", "Comparison", "Workflow", "FAQ",
                      "FullGuide", "Beginner", "Ultimate"}

# ── 정적 검색 가능성 점수 (뉴스 API 호출 없이 추정) ───────────────────────────

_SEARCHABILITY: dict[str, int] = {
    "ChatGPT": 95, "Claude": 90, "Gemini": 90, "NVIDIA": 90,
    "Claude Code": 85, "Bitcoin": 85, "Cursor": 82,
    "ETF": 80, "AI Agent": 80, "US Stocks": 78,
    "MCP": 75, "Semiconductor": 75, "AI Stocks": 72, "iPhone": 72, "TSLA": 72,
    "Perplexity": 70, "Galaxy": 70, "AdSense": 70, "Copilot": 70,
    "GPU": 68, "Midjourney": 68, "SaaS": 68,
    "Blog Monetization": 68, "Python Automation": 65, "n8n": 65,
    "Solo Development": 65, "AI Side Hustle": 65, "Docker": 65, "MacBook": 65, "Data Center": 65,
    "Grok": 65, "LangChain": 62, "NotebookLM": 62, "RAG": 60,
    "Zapier": 60, "Freelancing": 58, "Windsurf": 58, "Productivity": 57,
    "Make": 55, "Notion": 55, "Lovable": 55, "Startup": 52,
}
_DEFAULT_SEARCHABILITY = 45

# FullGuide 토픽 점수
_SEARCHABILITY.update({
    "ChatGPT Complete Guide": 92, "Claude Complete Guide": 88,
    "AI Tools Comparison": 85, "Claude Code Guide": 86,
    "Gemini Complete Guide": 83, "Blog Monetization Guide": 78,
    "Google AdSense Guide": 75, "n8n Automation Guide": 70,
    "Bitcoin Complete Guide": 80, "ETF Investing Guide": 78,
})
# Career 토픽 점수
_SEARCHABILITY.update({
    "Job Change Strategy": 60, "Salary Negotiation": 63, "AI Era Jobs": 55,
    "Remote Work": 52, "Side Project": 60, "Portfolio Building": 50,
    "Solo Entrepreneurship": 55, "Personal Branding": 48, "LinkedIn Strategy": 52, "Career Change": 55,
})
# Creator 토픽 점수
_SEARCHABILITY.update({
    "YouTube Algorithm": 68, "Newsletter Monetization": 58, "Blog SEO": 65,
    "Short-Form Content": 60, "Content Strategy": 54, "Digital Nomad": 56,
    "Online Course Creation": 52, "eBook Publishing": 50, "Community Management": 47,
})
# Programming 토픽 점수
_SEARCHABILITY.update({
    "Python": 78, "JavaScript": 75, "TypeScript": 68, "React": 70,
    "FastAPI": 60, "Docker": 65, "Git": 62, "REST API": 58,
    "SQL": 65, "Linux Commands": 55,
})
# Tools 토픽 점수
_SEARCHABILITY.update({
    "Notion": 55, "Figma": 60, "Obsidian": 48, "VS Code": 65,
    "Raycast": 45, "Arc Browser": 50, "Supabase": 55, "Vercel": 58,
})
# Crypto 토픽 점수
_SEARCHABILITY.update({
    "Ethereum": 78, "Ripple": 68, "Solana": 65, "DeFi": 58,
    "Crypto Investing": 72, "Web3": 55, "Altcoin": 60,
})

# ── 수익 관련성 ───────────────────────────────────────────────────────────────

_MONEY_HIGH = {
    "Bitcoin", "ETF", "US Stocks", "NVIDIA", "AI Stocks", "Semiconductor",
    "TSLA", "Data Center", "SaaS", "AdSense", "Blog Monetization",
}
_MONEY_MID = {
    "ChatGPT", "Gemini", "Claude", "Claude Code", "Cursor",
    "AI Agent", "n8n", "Zapier", "AI Side Hustle", "Solo Development",
    "Newsletter Monetization", "Solo Entrepreneurship", "Side Project", "Online Course Creation",
}


# ── 실시간 트렌드 토픽 (WordPress 전용, 2시간 캐시) ─────────────────────────

# 동적으로 추출된 토픽 집합 (점수 보너스용)
_wp_dynamic_topics: set[str] = set()

_WP_CATEGORIES = ["AI", "Automation", "Market", "Crypto", "Career",
                   "Creator", "Consumer Tech", "Programming", "Tools", "Guides"]


async def _fetch_hn_headlines() -> list[str]:
    """HN top 50 + best 30 제목 수집."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            top_r, best_r = await asyncio.gather(
                client.get("https://hacker-news.firebaseio.com/v0/topstories.json"),
                client.get("https://hacker-news.firebaseio.com/v0/beststories.json"),
                return_exceptions=True,
            )
            ids: list[int] = []
            for r in (top_r, best_r):
                if not isinstance(r, Exception):
                    ids.extend(r.json()[:30])
            ids = list(dict.fromkeys(ids))[:60]
            tasks = [client.get(f"https://hacker-news.firebaseio.com/v0/item/{i}.json")
                     for i in ids]
            results = await asyncio.gather(*tasks, return_exceptions=True)
        titles = []
        for res in results:
            if isinstance(res, Exception):
                continue
            try:
                t = res.json().get("title", "")
                if t and len(t) > 5:
                    titles.append(t[:120])
            except Exception:
                pass
        return titles
    except Exception as e:
        logger.debug("HN fetch failed: %s", e)
        return []


async def _fetch_reddit_headlines() -> list[str]:
    """Reddit 핫글 제목 수집 (실패 시 빈 리스트)."""
    subs = "artificial+ChatGPT+LocalLLaMA+MachineLearning+investing+stocks+CryptoCurrency+productivity"
    try:
        async with httpx.AsyncClient(
            timeout=8, headers={"User-Agent": "blog-topic-engine/1.0"},
        ) as client:
            r = await client.get(
                f"https://www.reddit.com/r/{subs}/hot.json", params={"limit": 40},
            )
            posts = r.json()["data"]["children"]
        return [p["data"]["title"] for p in posts if not p["data"].get("stickied")][:30]
    except Exception as e:
        logger.debug("Reddit fetch failed: %s", e)
        return []


async def _extract_wp_topics_gemini(headlines: list[str]) -> dict[str, list[str]]:
    """헤드라인에서 카테고리별 토픽명을 Gemini로 추출."""
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key or not headlines:
        return {}
    # 헤드라인 40개로 제한 — JSON 응답이 잘리지 않도록
    sample = headlines[:40]
    logger.info("WP Gemini extracting from %d headlines", len(sample))
    text = "\n".join(f"- {h}" for h in sample)
    cats_str = ", ".join(_WP_CATEGORIES)
    prompt = (
        f"Categorize these tech headlines into blog topics.\n"
        f"Categories: {cats_str}\n\n"
        f"Rules:\n"
        f"- Extract tool/technology/concept names as short topic phrases (2-4 words)\n"
        f"- Max 3 topics per category, no duplicates\n"
        f"- Omit categories with no matches\n"
        f"- Return ONLY a JSON object, no explanation\n\n"
        f"Example output: "
        f'{{\"AI\": [\"Claude 4 features\", \"local LLM setup\"], \"Programming\": [\"Rust async patterns\"]}}\n\n'
        f"Headlines:\n{text}"
    )
    try:
        client = genai.Client(api_key=api_key)
        response = await client.aio.models.generate_content(
            model=_GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=4096,
            ),
        )
        # finish_reason 확인
        try:
            finish = response.candidates[0].finish_reason
            if str(finish) not in ("FinishReason.STOP", "1", "STOP"):
                logger.warning("WP Gemini finish_reason=%s (may be truncated)", finish)
        except Exception:
            pass
        raw = response.text.strip() if response.text else ""
        if not raw:
            logger.warning("WP Gemini topic extract: empty response")
            return {}
        # JSON 블록 추출
        raw_clean = re.sub(r"^```(?:json)?\s*", "", raw)
        raw_clean = re.sub(r"\s*```\s*$", "", raw_clean).strip()
        try:
            result = json.loads(raw_clean)
        except json.JSONDecodeError:
            m = re.search(r"\{[\s\S]*\}", raw_clean)
            try:
                result = json.loads(m.group()) if m else {}
            except json.JSONDecodeError:
                result = {}
        if not result:
            logger.warning("WP Gemini topic extract: parse failed, raw=%r", raw[:400])
        # 값이 list[str]인지 검증
        filtered = {k: [t for t in v if isinstance(t, str) and len(t) >= 2]
                    for k, v in result.items() if isinstance(v, list)}
        logger.info("WP Gemini parsed: %d categories, sample=%s", len(filtered),
                    list(filtered.items())[:2])
        return filtered
    except Exception as e:
        logger.warning("WP Gemini topic extract failed: %s", e)
        return {}


async def _fetch_wp_dynamic_topics() -> dict[str, list[str]]:
    """HN + Reddit 헤드라인 → Gemini 카테고리 매핑. 2시간 캐시."""
    cache_key = "wp_dynamic_topics_v5"
    cached = cache.get(cache_key)
    if cached is not None:
        logger.debug("WP dynamic topics cache hit")
        return cached

    hn_titles, reddit_titles = await asyncio.gather(
        _fetch_hn_headlines(),
        _fetch_reddit_headlines(),
        return_exceptions=True,
    )
    all_titles: list[str] = []
    for src in (hn_titles, reddit_titles):
        if isinstance(src, list):
            all_titles.extend(src)

    logger.info("WP dynamic topics: %d headlines collected", len(all_titles))
    if not all_titles:
        return {}

    topics = await _extract_wp_topics_gemini(all_titles)
    total = sum(len(v) for v in topics.values())
    logger.info("WP dynamic topics extracted: %d topics across %d categories", total, len(topics))

    cache.set(cache_key, topics, ttl=7200)
    return topics


def _read_doc_folder() -> list[str]:
    """doc/ 폴더 텍스트 파일의 첫 줄을 키워드 힌트로 활용."""
    doc_dir = Path(__file__).parent.parent / "doc"
    topics: list[str] = []
    if not doc_dir.exists():
        return topics
    for f in sorted(doc_dir.iterdir()):
        if f.suffix in (".txt", ".md") and f.stat().st_size > 0:
            try:
                line = f.read_text(encoding="utf-8").strip().splitlines()[0]
                if line:
                    topics.append(line[:80])
            except Exception:
                pass
    return topics[:4]


def _get_popular_topics() -> list[str]:
    """DB 조회수 상위 포스트의 키워드를 재활용 후보로 반환."""
    from services.blog_db import get_recent_posts
    posts = sorted(
        get_recent_posts(limit=100),
        key=lambda p: p.get("view_count", 0),
        reverse=True,
    )
    return [p["keyword"] for p in posts[:6] if p.get("keyword")]


# ── Canonical Topic 별칭 (동일 Entity, 다른 표기) ────────────────────────────
# TOPIC_AREAS는 고정 풀이라 전체 엔티티가 유한하다 — DB에 canonical_topic 컬럼을
# 새로 만들어 발행 파이프라인 3곳(네이버/트렌드랩/가이드)의 저장 로직을 전부 고치는
# 대신, 반복 방지 비교 시점에만 "topic이 keyword 안에 어떤 표기로 등장하는지"를
# 이 표로 확인한다 (읽기 시점 정규화 — 쓰기 경로는 건드리지 않음).
# 여기 없는 토픽은 기존처럼 원문 그대로 substring 비교된다.
_TOPIC_ALIASES: dict[str, list[str]] = {
    "Bitcoin":              ["bitcoin", "btc", "비트코인"],
    "Bitcoin Complete Guide": ["bitcoin", "btc", "비트코인"],
    "NVIDIA":               ["nvidia", "엔비디아"],
    "ChatGPT":              ["chatgpt", "chat gpt", "gpt", "챗gpt", "챗지피티"],
    "ChatGPT Complete Guide": ["chatgpt", "chat gpt", "gpt"],
    "Claude":               ["claude", "claude ai", "클로드"],
    "Claude Code":          ["claude code", "클로드 코드", "클로드코드"],
    "Claude Complete Guide": ["claude", "클로드"],
    "Claude Code Guide":    ["claude code", "클로드 코드", "클로드코드"],
    "Gemini":               ["gemini", "제미나이", "제미니"],
    "Gemini Complete Guide": ["gemini", "제미나이", "제미니"],
    "AdSense":              ["adsense", "google adsense", "애드센스", "구글 애드센스"],
    "Google AdSense Guide": ["adsense", "google adsense", "애드센스"],
    "Ethereum":             ["ethereum", "eth", "이더리움"],
    "ETF":                  ["etf", "이티에프"],
    "ETF Investing Guide":  ["etf", "이티에프"],
    "TSLA":                 ["tsla", "tesla", "테슬라"],
    "iPhone":               ["iphone", "아이폰"],
    "Galaxy":               ["galaxy", "갤럭시"],
    "MacBook":              ["macbook", "맥북"],
}


def _topic_in_keyword(topic: str, keyword: str) -> bool:
    """topic이 keyword 안에 (원문/영문/음역/약어 등 등록된 별칭 포함) 등장하는지 확인."""
    kw_lower = keyword.lower()
    aliases = _TOPIC_ALIASES.get(topic, [topic.lower()])
    return any(alias in kw_lower for alias in aliases)


def _validate_topic_aliases() -> None:
    """_TOPIC_ALIASES가 TOPIC_AREAS와 어긋나지 않았는지 모듈 로드 시 1회 검증.

    토픽 이름이 바뀌거나 삭제됐는데 별칭 키를 안 고치면, 그 별칭은 조용히 죽은
    채로 남아 반복 방지가 무력화된다 (에러 없이 그냥 매칭이 항상 실패). 여기서
    미리 경고 로그를 남겨 alias 딕셔너리가 TOPIC_AREAS와 동기화되도록 한다.
    """
    all_topics = {t for area in TOPIC_AREAS.values() for t in area["topics"]}
    orphaned = [alias_key for alias_key in _TOPIC_ALIASES if alias_key not in all_topics]
    if orphaned:
        logger.warning(
            "Topic alias 동기화 경고 — TOPIC_AREAS에 없는 _TOPIC_ALIASES 키 발견 "
            "(토픽명 변경/삭제 후 별칭 미수정 가능성): %s",
            orphaned,
        )


_validate_topic_aliases()


# ── 다양성 체크 (plan.md §다양성 규칙) ───────────────────────────────────────

def _jaccard(a: str, b: str) -> float:
    wa, wb = set(a.lower().split()), set(b.lower().split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def _too_similar(keyword: str, recent_keywords: list[str], threshold: float = TOPIC_DUPLICATE_THRESHOLD) -> bool:
    """후보 키워드가 과거 실제 사용된 keyword(원본 토픽, AI 확장 전)와 Jaccard 유사도 threshold 이상이면 True.

    과거에는 AI가 확장한 최종 title과 비교해서, 짧은 후보 키워드와 단어 수 차이가
    커 유사도가 항상 낮게 나와 중복을 못 걸렀다 (예: "Bitcoin tutorial guide" vs
    "Your First Bitcoin Tutorial Guide: How to Buy & Secure It" → Jaccard ~0.27).
    """
    return any(_jaccard(keyword, kw) >= threshold for kw in recent_keywords)


def _days_since(created_at: str) -> float | None:
    try:
        return (datetime.now() - datetime.fromisoformat(created_at)).total_seconds() / 86400
    except (ValueError, TypeError):
        return None


# ── 점수 계산 (plan.md §최종 목표 — 5개 항목 + 반복 패널티) ──────────────────

def _score(
    topic: str,
    intent: str,
    category: str,
    recent_keywords: list[str],
    recent_categories: list[str],
    recent_created_at: list[str] | None = None,
) -> float:
    """
    5차원 점수 합산 - 반복 패널티.
    - 검색 가능성 (40): 정적 인기도 테이블
    - 수익 관련성 (25): 투자·AI 도구 우선
    - 장기 가치   (20): 에버그린 인텐트 우선
    - 반복 패널티: 최근 TOPIC_PENALTY_DAYS일 내 동일 토픽 → TOPIC_PENALTY 감점
                   최근 ENTITY_PENALTY_DAYS일 내 동일 토픽 → ENTITY_PENALTY 추가 감점
                   최근 CATEGORY_PENALTY_DAYS일 내 동일 카테고리 → CATEGORY_PENALTY 감점
    - 실시간 트렌드 보너스
    """
    search   = _SEARCHABILITY.get(topic, _DEFAULT_SEARCHABILITY) * 0.40
    money    = 25.0 if topic in _MONEY_HIGH else (15.0 if topic in _MONEY_MID else 7.0)
    longterm = 20.0 if intent in _EVERGREEN_INTENTS else 10.0

    recent_created_at = recent_created_at or []

    penalty = 0.0
    # recent_keywords/recent_created_at은 created_at DESC 순서로 정렬돼 있으므로
    # 첫 매치가 가장 최근 발행 — 그 하나만 반영해 과도한 중복 가산을 피한다.
    for kw, created in zip(recent_keywords, recent_created_at):
        if not _topic_in_keyword(topic, kw):
            continue
        age = _days_since(created)
        if age is None:
            continue
        if age <= ENTITY_PENALTY_DAYS:
            penalty += ENTITY_PENALTY
        if age <= TOPIC_PENALTY_DAYS:
            penalty += TOPIC_PENALTY
        break

    for cat, created in zip(recent_categories, recent_created_at):
        if cat.split("_")[0] != category:
            continue
        age = _days_since(created)
        if age is None:
            continue
        if age <= CATEGORY_PENALTY_DAYS:
            penalty += CATEGORY_PENALTY
        break

    # 실시간 트렌드에서 추출된 토픽은 보너스 증가
    trend_bonus = 20.0 if topic in _wp_dynamic_topics else 0.0
    return search + money + longterm + trend_bonus - penalty


# ── 메인 함수 ─────────────────────────────────────────────────────────────────

async def pick_topic(
    lang: str = "ko",
    exclude_topics: list[str] | None = None,
    exclude_categories: set[str] | None = None,
    platform: str = "wordpress",
) -> tuple[str, str]:
    """
    Topic + Intent 조합에서 최고 점수 키워드 1개 선정.

    plan.md 기준:
    - 후보 50개 생성 → 5개 점수 → 상위 1개 반환
    - 70% 유사도 / 같은 topic 3연속 / 같은 category 4연속 방지

    최근 이력은 동일 파이프라인(platform+language) 기준으로만 조회한다 — 네이버/
    트렌드랩 등 다른 파이프라인 글이 섞이면 최근 이력이 희석되어 반복 방지가
    무력화되기 때문 (예: 영어 가이드는 하루 1회뿐인데 그 사이 네이버 글이 계속
    쌓이면 며칠 전 발행한 주제도 "최근"으로 안 잡힘).

    Returns:
        (keyword, category)
        예) ("Claude Code 자동화 방법", "Automation")
            ("how to use Claude Code", "Automation")
    """
    from services.blog_db import get_diversity_data

    diversity = get_diversity_data(
        days=TOPIC_RECENT_HISTORY_DAYS,
        platform=platform,
        language=lang,
        limit=TOPIC_RECENT_HISTORY_LIMIT,
    )
    recent_keywords   = diversity["keywords"]
    recent_categories = diversity["categories"]
    recent_created_at = diversity["created_at"]

    exclude_set  = set(exclude_topics or [])
    exclude_cats = set(exclude_categories or set())

    # 실시간 트렌드 토픽 주입 (2시간 캐시)
    global _wp_dynamic_topics
    dynamic = await _fetch_wp_dynamic_topics()
    _wp_dynamic_topics = set()
    for cat, topics in dynamic.items():
        if cat in TOPIC_AREAS:
            # 정적 풀 앞에 실시간 토픽 삽입 (최대 5개)
            existing = TOPIC_AREAS[cat]["topics"]
            new_topics = [t for t in topics if t not in existing][:5]
            TOPIC_AREAS[cat]["topics"] = new_topics + existing
            _wp_dynamic_topics.update(new_topics)

    # Trending 풀: doc/ 폴더 + 인기 포스트 재활용
    trending: list[str] = list(dynamic.get("Trending", []))
    trending.extend(_read_doc_folder())
    trending.extend(_get_popular_topics())
    TOPIC_AREAS["Trending"]["topics"] = list(dict.fromkeys(trending))[:12]

    cats      = list(TOPIC_AREAS.keys())
    weights   = [TOPIC_AREAS[c]["weight"] for c in cats]
    templates = _INTENT_KR if lang == "ko" else _INTENT_EN

    best_keyword  = ""
    best_category = "AI"
    best_score    = -1.0
    seen: set[str] = set()

    for _ in range(60):
        cat   = random.choices(cats, weights=weights, k=1)[0]
        pool  = TOPIC_AREAS[cat]["topics"]
        if not pool:
            continue
        topic  = random.choice(pool)
        intent = random.choice(list(templates.keys()))
        key    = f"{topic}::{intent}"

        # exclude_set은 전체 키워드 문자열 — 별칭(영문/음역/약어) 포함 비교
        if key in seen or any(_topic_in_keyword(topic, ex) for ex in exclude_set) or cat in exclude_cats:
            continue
        seen.add(key)

        # 다양성 규칙 — 별칭 포함 비교 & category 접미사 제거
        if sum(1 for kw in recent_keywords[:6] if _topic_in_keyword(topic, kw)) >= 2:
            continue
        cat_norm_recent = [c.split("_")[0] for c in recent_categories[:7]]
        if cat_norm_recent.count(cat) >= 3:
            continue

        keyword = templates[intent].format(topic=topic)
        if _too_similar(keyword, recent_keywords):
            continue

        sc = _score(topic, intent, cat, recent_keywords, recent_categories, recent_created_at)
        if sc > best_score:
            best_score    = sc
            best_keyword  = keyword
            best_category = cat

    if not best_keyword:
        fallback = templates["HowTo"].format(topic="Claude Code")
        logger.warning("Topic engine fallback to default keyword")
        return fallback, "Automation"

    logger.info(
        "Topic engine picked: '%s' (category=%s, score=%.1f, lang=%s)",
        best_keyword, best_category, best_score, lang,
    )
    return best_keyword, best_category


# ── WordPress 전용 alias ───────────────────────────────────────────────────────

async def pick_topic_wp(
    lang: str = "ko",
    exclude_topics: list[str] | None = None,
    exclude_categories: set[str] | None = None,
) -> tuple[str, str]:
    """WordPress SEO 특화 토픽 선정 (pick_topic 래퍼)."""
    return await pick_topic(lang=lang, exclude_topics=exclude_topics, exclude_categories=exclude_categories)


# ── 엔티티 필터 블랙리스트 ─────────────────────────────────────────────────────
# NER이 직책·일반명사·단순 지시어를 잘못 추출할 때 걸러냄
_ENTITY_BLACKLIST: set[str] = {
    # 직책·직위
    "부장", "차장", "과장", "대리", "사원", "팀장", "이사", "상무", "전무", "대표",
    "회장", "사장", "원장", "소장", "실장", "국장", "장관", "차관", "대통령",
    "의원", "의장", "검사", "판사", "변호사", "교수", "총장", "학장",
    "감독", "코치", "단장", "구단주",
    # 일반 호칭·관계
    "김씨", "이씨", "박씨", "씨", "군", "양", "님",
    "남편", "아내", "남친", "여친", "부모", "아버지", "어머니", "형", "동생",
    # 기관·조직 일반명사
    "정부", "경찰", "검찰", "법원", "국회", "청와대", "대검", "고검",
    "병원", "학교", "회사", "기업", "단체", "협회", "연맹", "조합",
    # 기타 일반어
    "관계자", "관계자들", "네티즌", "누리꾼", "시청자", "독자", "팬",
    "측", "입장", "발표", "논란", "사건", "사고", "이슈",
}

# 직책 접미사 패턴 (예: 김부장, 이과장, 박대리)
_TITLE_SUFFIXES = ("부장", "차장", "과장", "대리", "사원", "팀장", "이사", "상무",
                   "전무", "대표", "회장", "사장", "원장", "소장", "실장", "국장",
                   "검사", "판사", "의원", "감독", "코치", "단장")


def _is_valid_entity(name: str) -> bool:
    """직책명·일반명사·속어·너무 짧은 단어를 걸러냄."""
    if len(name) < 2:
        return False
    # 한글 2자 이하는 직책/일반명사일 가능성이 높음 (예: 이씨, 박씨)
    korean_chars = sum(1 for c in name if '가' <= c <= '힣')
    if korean_chars > 0 and len(name) < 3:
        return False
    if name in _ENTITY_BLACKLIST:
        return False
    # "김부장", "이과장" 등 성(1자) + 직책
    if len(name) >= 3 and name[1:] in _TITLE_SUFFIXES:
        return False
    # 숫자만 있는 경우
    if name.isdigit():
        return False
    return True


# ── Naver 전용 토픽 그룹 (뉴스 수집용 쿼리, 고정 인물 아님) ────────────────────

_NAVER_TOPIC_GROUPS: dict[str, list[str]] = {
    "연예":  ["아이돌 연예인", "드라마 배우", "예능 방송"],
    "스포츠": ["한국 스포츠 선수", "야구 축구 경기", "스포츠 이슈"],
    "IT":   ["스마트폰 앱 서비스", "IT 기업 플랫폼", "테크 제품"],
    "AI":   ["ChatGPT 클로드 제미나이 AI 모델", "AI 반도체 엔비디아 GPU", "오픈AI 구글 마이크로소프트 AI"],
    "주식":  ["주식 코인 투자", "코스피 기업 주가", "미국주식 ETF"],
    "이슈":  ["사회 논란 이슈", "온라인 화제", "커뮤니티 갑질"],
    "생활":  ["부동산 물가 생활비", "건강 다이어트 육아", "소비 트렌드"],
    "자동차": ["전기차 자동차 신차", "테슬라 현대 기아", "자율주행"],
    "쇼핑":  ["쿠팡 다이소 쇼핑", "명품 패션 트렌드", "해외직구"],
    "게임":  ["게임 e스포츠", "롤 배그 신작 게임", "게임 논란"],
}

_NAVER_INTENTS: dict[str, str] = {
    "Issue":      "{topic}가 이슈인 이유",
    "Reaction":   "{topic} 반응 총정리",
    "Summary":    "{topic} 상황 정리",
    "Hot":        "요즘 {topic} 화제가 된 이유",
    "Prediction": "{topic} 앞으로 어떻게 될까",
    "Top5":       "{topic} 핵심 5가지",
    "Impact":     "{topic} 실제 영향은",
    "Debate":     "{topic} 논란 총정리",
    "Opinion":    "{topic} 솔직한 생각",
    "Story":      "{topic} 지금 상황",
}

# 인텐트별 CTR 잠재력 점수
_NAVER_INTENT_CTR: dict[str, float] = {
    "Hot": 25.0, "Issue": 23.0, "Debate": 22.0, "Reaction": 21.0,
    "Top5": 20.0, "Impact": 17.0, "Summary": 16.0,
    "Prediction": 15.0, "Opinion": 14.0, "Story": 13.0,
}

# 카테고리별 클릭률 잠재력
_NAVER_CAT_CTR: dict[str, float] = {
    "연예": 30.0, "스포츠": 28.0, "이슈": 25.0,
    "AI": 23.0, "IT": 22.0, "주식": 20.0,
    "자동차": 18.0, "생활": 16.0, "쇼핑": 14.0, "게임": 12.0,
}


_GEMINI_MODEL = "gemini-2.5-flash"


def _is_recent(pub_date_str: str, now_ts: float) -> bool:
    """RFC822 pubDate가 24시간 이내이면 True. 파싱 실패 시 False."""
    if not pub_date_str:
        return False
    try:
        import email.utils
        ts = email.utils.parsedate_to_datetime(pub_date_str).timestamp()
        return (now_ts - ts) < 86400
    except Exception:
        return False


async def _extract_entities_gemini(articles: list[dict], category: str) -> dict[str, int]:
    """기사 제목 목록을 Gemini로 분석해 엔티티명→출현횟수 dict 반환."""
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        return {}
    titles = "\n".join(a["title"] for a in articles[:300])
    # 카테고리별 최소 등장 횟수 — 브랜드/차 카테고리는 항상 언급되므로 기준 높임
    _MIN_COUNT = {"연예": 3, "스포츠": 4, "이슈": 4, "게임": 4,
                  "자동차": 7, "IT": 7, "주식": 7, "생활": 6, "쇼핑": 6, "AI": 6}
    min_count = _MIN_COUNT.get(category, 5)
    prompt = (
        f"아래는 오늘 네이버 {category} 뉴스 제목 목록입니다.\n\n"
        "다음 조건을 모두 만족하는 항목만 추출하세요:\n"
        f"1. 최소 {min_count}회 이상 등장한 것만\n"
        "2. 실제로 존재하는 공인의 실명 또는 공식 단체명/브랜드명/작품명\n"
        "   - 허용: 연예인 이름(예: 장원영, 뉴진스, 손흥민), 기업명(예: 삼성, 애플), 작품명, 스포츠팀명\n"
        "   - 제외: 직책+성씨 조합(예: 김부장, 이과장), 인터넷 속어/유행어, 일반 명사, 짧은 형용사\n"
        "3. '특정 사건·신제품·발표·논란'과 연결된 것만 — 뉴스에 단순히 자주 나오는 브랜드(예: 포르쉐, 벤츠)는 제외\n"
        "4. 반드시 실명 또는 공식 명칭이어야 함 (가명, 닉네임, 별명은 제외)\n"
        "5. 동명이인 주의: 동일 이름이 서로 다른 직업·소속의 여러 인물(예: 배우 A + 금융인 A + 운동선수 A)에 분산된 경우 제외.\n"
        "   → 하나의 특정 인물을 가리키는 기사가 전체의 2/3 이상을 차지할 때만 포함\n\n"
        '출력: {"엔티티명": 횟수} JSON만. 설명 절대 금지.\n\n'
        f"제목 목록:\n{titles}"
    )
    try:
        client = genai.Client(api_key=api_key)
        response = await client.aio.models.generate_content(
            model=_GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.1,
                max_output_tokens=8192,
            ),
        )
        raw = response.text.strip()
        # 코드블록 제거
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        try:
            m = re.search(r"\{[\s\S]*\}", raw)
            return json.loads(m.group() if m else raw)
        except json.JSONDecodeError:
            # JSON이 잘렸어도 완성된 "key": number 쌍은 추출
            pairs = re.findall(r'"([^"]+)"\s*:\s*(\d+)', raw)
            if pairs:
                logger.info("Gemini NER partial extract: %d entities", len(pairs))
                return {name: int(cnt) for name, cnt in pairs}
            logger.warning("Gemini NER parse failed, raw[:150]=%s", raw[:150])
            return {}
    except Exception as e:
        logger.warning("Gemini NER failed: %s", e)
        return {}


def _score_entities(entity_counts: dict[str, int], articles: list[dict]) -> list[dict]:
    """빈도(60%) + 최신성(25%) + 다양성(15%) 점수화. 직책·일반명사 제외."""
    if not entity_counts:
        return []
    # 블랙리스트 필터 먼저 적용
    filtered = {n: c for n, c in entity_counts.items() if _is_valid_entity(n)}
    if not filtered:
        logger.debug("All entities filtered by blacklist, count=%d", len(entity_counts))
        return []
    max_count = max(filtered.values(), default=1)
    now_ts = time.time()
    total = max(len(articles), 1)
    scored = []
    for name, count in filtered.items():
        freq = (count / max_count) * 60
        matching = [a for a in articles if name in a["title"]]
        recent_count = sum(1 for a in matching if _is_recent(a.get("pub_date", ""), now_ts))
        recency = (recent_count / max(len(matching), 1)) * 25
        diversity = (len(matching) / total) * 15
        scored.append({"name": name, "count": count, "score": freq + recency + diversity})
    return scored


async def _fetch_category_entities(category: str) -> list[dict]:
    """카테고리 뉴스 최대 300건 수집 → Gemini NER → TOP10 엔티티 반환. 1시간 캐시."""
    cache_key = f"naver_entities_v2:{category}"
    cached = cache.get(cache_key)
    if cached is not None:
        logger.debug("Entity cache hit: %s", category)
        return cached

    queries = _NAVER_TOPIC_GROUPS.get(category, [category + " 뉴스"])
    results = await asyncio.gather(
        *[search_news(q, display=100, sort="date") for q in queries],
        return_exceptions=True,
    )
    articles = [a for r in results if isinstance(r, list) for a in r]
    logger.info("Entity fetch: category=%s, articles=%d", category, len(articles))

    if not articles:
        return []

    entity_counts = await _extract_entities_gemini(articles, category)
    scored = _score_entities(entity_counts, articles)
    top10 = sorted(scored, key=lambda x: x["score"], reverse=True)[:10]
    cache.set(cache_key, top10, ttl=3600)
    return top10


def _naver_hot_score(
    topic: str,
    intent: str,
    category: str,
    recent_keywords: list[str],
    recent_categories: list[str],
) -> float:
    ctr       = _NAVER_CAT_CTR.get(category, 15.0)
    int_score = _NAVER_INTENT_CTR.get(intent, 14.0)
    fresh     = (
        0.0  if topic in recent_keywords[:3]
        else 5.0  if topic in recent_keywords[:8]
        else 15.0
    )
    cat_count = recent_categories[:14].count(category)
    cat_pen   = 0.0 if cat_count >= 4 else (7.0 if cat_count >= 2 else 15.0)
    return ctr + int_score + fresh + cat_pen


# 최소 언급 횟수가 적은 엔티티에 제한적으로 허용하는 인텐트
_INTENT_MIN_COUNT: dict[str, int] = {
    "Debate":     8,   # 논란 총정리 — 실제 논란 없으면 의미 없음
    "Opinion":    6,   # 솔직한 생각 — 충분한 화제성 필요
    "Reaction":   6,   # 반응 총정리 — 반응이 있어야 쓸 수 있음
    "Prediction": 5,   # 전망 — 어느 정도 화제여야 함
}

# Opinion 인텐트는 "써보니 어떤가/장단점 정리" 후기 포맷(generator._INTENT_GUIDE 참고)이라
# 실제 사용 경험이 가능한 제품/서비스 카테고리에서만 허용한다.
# 자동차·이슈·주식·AI·스포츠·연예처럼 엔티티가 브랜드·기업·인물인 카테고리에 적용하면
# "후기" 소재가 없어 뉴스 기사를 억지로 짜깁기한 알맹이 없는 글이 나온다.
_OPINION_ALLOWED_CATEGORIES = {"쇼핑", "생활", "게임", "IT"}


async def pick_topic_naver() -> tuple[str, str, str]:
    """
    네이버 블로그 전용 실시간 엔티티 기반 토픽 선정.
    오늘 네이버 뉴스에서 가장 많이 언급된 인물/그룹/작품/브랜드를 동적 추출.
    Returns: (keyword, category, intent)
    """
    from services.blog_db import get_diversity_data

    diversity       = get_diversity_data(days=30)
    recent_keywords = diversity["keywords"]

    cats    = list(_NAVER_TOPIC_GROUPS.keys())
    weights = [_NAVER_CAT_CTR.get(c, 15.0) for c in cats]
    cat     = random.choices(cats, weights=weights, k=1)[0]

    entities = await _fetch_category_entities(cat)
    if not entities:
        logger.warning("Entity engine fallback: category=%s", cat)
        return f"요즘 {cat} 이슈", cat, "Hot"

    # 최소 점수 필터 — 너무 마이너한 엔티티(score < 15) 제외
    viable = [e for e in entities if e.get("score", 0) >= 15]
    if not viable:
        viable = entities  # 모두 저점수면 그냥 사용

    recent_name_set = set(recent_keywords[:10])
    filtered = [e for e in viable if e["name"] not in recent_name_set]
    pool = filtered if filtered else viable
    chosen = random.choices(pool, weights=[e["score"] for e in pool], k=1)[0]
    entity_count = chosen.get("count", 0)

    # 인텐트 선택 — 언급 횟수가 적으면 논란/의견 인텐트 제외
    available_intents = {
        intent: weight
        for intent, weight in _NAVER_INTENT_CTR.items()
        if entity_count >= _INTENT_MIN_COUNT.get(intent, 0)
        and (intent != "Opinion" or cat in _OPINION_ALLOWED_CATEGORIES)
    }
    if not available_intents:
        available_intents = {"Hot": 25.0, "Issue": 23.0, "Summary": 16.0}

    intent = random.choices(
        list(available_intents.keys()),
        weights=list(available_intents.values()),
        k=1,
    )[0]
    keyword = _NAVER_INTENTS[intent].format(topic=chosen["name"])
    logger.info("entity_count=%d, intent=%s (min_required=%d)",
                entity_count, intent, _INTENT_MIN_COUNT.get(intent, 0))
    logger.info(
        "Naver entity picked: '%s' (entity=%s, category=%s, score=%.1f)",
        keyword, chosen["name"], cat, chosen["score"],
    )
    return keyword, cat, intent
