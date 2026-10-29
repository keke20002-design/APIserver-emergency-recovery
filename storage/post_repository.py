"""Post Repository — 단일 DB 접근 계층."""
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "data" / "blog.db"

# 내부 status 값
class Status:
    READY     = "READY"
    REVIEW    = "REVIEW"
    APPROVED  = "APPROVED"
    PUBLISHED = "PUBLISHED"
    FAILED    = "FAILED"
    ARCHIVED  = "ARCHIVED"

# WordPress/레거시 status → 내부 status 매핑
_STATUS_MAP: dict[str, str] = {
    "publish":   Status.PUBLISHED,
    "published": Status.PUBLISHED,
    "draft":     Status.READY,
    "private":   Status.READY,
    "failed":    Status.FAILED,
}


def _normalize_status(s: str) -> str:
    """레거시 status 값을 내부 Status 값으로 정규화."""
    return _STATUS_MAP.get(s.lower(), s.upper())


class PostRepository:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path

    def _conn(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    # ── 초기화 & 마이그레이션 ──────────────────────────────────────────────────

    def init_db(self) -> None:
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS blog_posts (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    keyword          TEXT NOT NULL,
                    topic            TEXT NOT NULL,
                    title            TEXT NOT NULL,
                    wp_post_id       INTEGER,
                    wp_url           TEXT,
                    status           TEXT DEFAULT 'READY',
                    error_msg        TEXT,
                    platform         TEXT DEFAULT 'wordpress',
                    language         TEXT DEFAULT 'ko',
                    thumbnail_url    TEXT,
                    meta_description TEXT,
                    source           TEXT,
                    intent           TEXT,
                    ready_at         TEXT,
                    reviewed_at      TEXT,
                    created_at       TEXT DEFAULT (datetime('now')),
                    published_at     TEXT,
                    view_count       INTEGER DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS blog_keywords (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    keyword    TEXT UNIQUE NOT NULL,
                    topic      TEXT NOT NULL,
                    used_at    TEXT,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS published_documents (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    doc_hash   TEXT NOT NULL,
                    keyword    TEXT NOT NULL,
                    wp_post_id INTEGER,
                    wp_url     TEXT,
                    created_at TEXT DEFAULT (datetime('now')),
                    UNIQUE(doc_hash, keyword)
                )
            """)
            conn.commit()

            # ── 기존 DB 컬럼 추가 마이그레이션 ──────────────────────────────
            new_cols = [
                ("view_count",       "INTEGER DEFAULT 0"),
                ("platform",         "TEXT DEFAULT 'wordpress'"),
                ("language",         "TEXT DEFAULT 'ko'"),
                ("thumbnail_url",    "TEXT"),
                ("meta_description", "TEXT"),
                ("source",           "TEXT"),
                ("intent",           "TEXT"),
                ("ready_at",         "TEXT"),
                ("reviewed_at",      "TEXT"),
                ("content",          "TEXT"),
                ("tags",             "TEXT"),
                ("summary",          "TEXT"),
                ("traffic_score",    "INTEGER"),
                ("competition",      "INTEGER"),
                ("trend_level",      "TEXT"),
                ("score_reason",     "TEXT"),
                ("ctr_score",        "INTEGER"),
                ("seo_score",        "INTEGER"),
                ("ai_risk",          "TEXT"),
                ("dopamine_score",   "INTEGER"),
                ("hub",              "TEXT"),
                ("subtopic_key",     "TEXT"),
            ]
            for col, defn in new_cols:
                try:
                    conn.execute(f"ALTER TABLE blog_posts ADD COLUMN {col} {defn}")
                    conn.commit()
                except Exception:
                    pass  # 이미 존재하는 컬럼

            # ── 레거시 status 값 정규화 ───────────────────────────────────────
            conn.execute("UPDATE blog_posts SET status='PUBLISHED' WHERE status='publish'")
            conn.execute("UPDATE blog_posts SET status='FAILED'    WHERE status='failed'")
            conn.execute("UPDATE blog_posts SET status='READY'     WHERE status='draft'")
            conn.commit()

        logger.info("PostRepository initialized at %s", self.db_path)

    # ── 키워드 관리 ───────────────────────────────────────────────────────────

    def is_keyword_used(self, keyword: str, months: int = 6) -> bool:
        cutoff = (datetime.now() - timedelta(days=months * 30)).isoformat()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM blog_keywords WHERE keyword = ? AND used_at > ?",
                (keyword, cutoff),
            ).fetchone()
        return row is not None

    def mark_keyword_used(self, keyword: str, topic: str) -> None:
        now = datetime.now().isoformat()
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO blog_keywords (keyword, topic, used_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(keyword) DO UPDATE SET used_at = excluded.used_at, topic = excluded.topic""",
                (keyword, topic, now),
            )
            conn.commit()

    def get_used_keywords(self, limit: int = 100) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT keyword, topic, used_at FROM blog_keywords ORDER BY used_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── 포스트 저장 ───────────────────────────────────────────────────────────

    def save_post(
        self,
        keyword: str,
        topic: str,
        title: str,
        wp_post_id: int | None = None,
        wp_url: str | None = None,
        status: str = Status.READY,
        error_msg: str | None = None,
        platform: str = "wordpress",
        language: str = "ko",
        thumbnail_url: str | None = None,
        meta_description: str | None = None,
        source: str | None = None,
        intent: str | None = None,
        content: str | None = None,
        tags: list[str] | None = None,
        summary: list[str] | None = None,
        traffic_score: int | None = None,
        competition: int | None = None,
        trend_level: str | None = None,
        score_reason: list[str] | None = None,
        ctr_score: int | None = None,
        seo_score: int | None = None,
        ai_risk: str | None = None,
        dopamine_score: int | None = None,
        hub: str | None = None,
        subtopic_key: str | None = None,
    ) -> int:
        """포스트 로그 저장. status는 내부 Status 값 또는 레거시 값 모두 허용."""
        import json as _json
        status = _normalize_status(status)
        now = datetime.now().isoformat()
        published_at  = now if status == Status.PUBLISHED else None
        ready_at      = now if status == Status.READY     else None
        tags_json     = _json.dumps(tags, ensure_ascii=False) if tags else None
        summary_json  = _json.dumps(summary, ensure_ascii=False) if summary else None
        reason_json   = _json.dumps(score_reason, ensure_ascii=False) if score_reason else None

        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO blog_posts
                   (keyword, topic, title, wp_post_id, wp_url, status, error_msg,
                    platform, language, thumbnail_url, meta_description,
                    source, intent, published_at, ready_at, content, tags, summary,
                    traffic_score, competition, trend_level, score_reason,
                    ctr_score, seo_score, ai_risk, dopamine_score, hub, subtopic_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (keyword, topic, title, wp_post_id, wp_url, status, error_msg,
                 platform, language, thumbnail_url, meta_description,
                 source, intent, published_at, ready_at, content, tags_json, summary_json,
                 traffic_score, competition, trend_level, reason_json,
                 ctr_score, seo_score, ai_risk, dopamine_score, hub, subtopic_key),
            )
            conn.commit()
            return cur.lastrowid

    # ── 포스트 조회 ───────────────────────────────────────────────────────────

    def get_posts(self, limit: int = 20) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM blog_posts ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_posts_by_platform(self, platform: str, status: str | None = None, limit: int = 50) -> list[dict]:
        with self._conn() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM blog_posts WHERE platform=? AND status=? ORDER BY created_at DESC LIMIT ?",
                    (platform, _normalize_status(status), limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM blog_posts WHERE platform=? ORDER BY created_at DESC LIMIT ?",
                    (platform, limit),
                ).fetchall()
        return [dict(r) for r in rows]

    def get_posts_filtered(
        self,
        platform: str | None = None,
        source: str | None = None,
        exclude_source: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """platform/source/status 조합 필터링."""
        clauses, params = [], []
        if platform:
            clauses.append("platform = ?"); params.append(platform)
        if source:
            clauses.append("source = ?"); params.append(source)
        if exclude_source:
            clauses.append("(source IS NULL OR source != ?)"); params.append(exclude_source)
        if status:
            clauses.append("status = ?"); params.append(_normalize_status(status))
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM blog_posts {where} ORDER BY created_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [dict(r) for r in rows]

    def get_recent_keyword_set(self, days: int = 7) -> set[str]:
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT keyword FROM blog_posts WHERE created_at > ? AND status != ?",
                (cutoff, Status.FAILED),
            ).fetchall()
        return {row["keyword"].lower() for row in rows}

    def get_related_posts(
        self,
        keyword: str,
        category: str,
        exclude_wp_id: int | None = None,
        limit: int = 3,
    ) -> list[dict]:
        words = [w for w in keyword.replace(",", " ").split() if len(w) > 1]
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT keyword, title, wp_url FROM blog_posts
                   WHERE status = ?
                   AND wp_url IS NOT NULL
                   AND wp_post_id != ?
                   AND topic = ?
                   ORDER BY view_count DESC, created_at DESC
                   LIMIT 30""",
                (Status.PUBLISHED, exclude_wp_id or 0, category),
            ).fetchall()
        posts = [dict(r) for r in rows]

        def _overlap(post: dict) -> float:
            pw = set(post["keyword"].replace(",", " ").split())
            qw = set(words)
            if not pw or not qw:
                return 0.0
            return len(pw & qw) / len(pw | qw)

        posts.sort(key=_overlap, reverse=True)
        return [p for p in posts[:limit] if p.get("wp_url") and p.get("title")]

    def get_hub_used_subtopics(self, hub: str) -> set[str]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT DISTINCT subtopic_key FROM blog_posts
                   WHERE hub = ? AND subtopic_key IS NOT NULL AND status != ?""",
                (hub, Status.FAILED),
            ).fetchall()
        return {r["subtopic_key"] for r in rows}

    def get_hub_post_counts(self, language: str = "en") -> dict[str, int]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT hub, COUNT(*) as n FROM blog_posts
                   WHERE hub IS NOT NULL AND language = ? AND status != ?
                   GROUP BY hub""",
                (language, Status.FAILED),
            ).fetchall()
        return {r["hub"]: r["n"] for r in rows}

    def get_hub_related_posts(self, hub: str, exclude_wp_id: int | None = None, limit: int = 3) -> list[dict]:
        """같은 허브 내 발행글만 — 형제 아티클로 최신순 반환 (Jaccard 불필요, 이미 허브로 고관련)."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT keyword, title, wp_url FROM blog_posts
                   WHERE status = ? AND wp_url IS NOT NULL AND hub = ? AND wp_post_id != ?
                   ORDER BY created_at DESC LIMIT ?""",
                (Status.PUBLISHED, hub, exclude_wp_id or 0, limit),
            ).fetchall()
        posts = [dict(r) for r in rows]
        return [p for p in posts if p.get("wp_url") and p.get("title")]

    def get_last_post_hub(self, language: str = "en") -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                """SELECT hub FROM blog_posts WHERE hub IS NOT NULL AND language = ? AND status != ?
                   ORDER BY created_at DESC LIMIT 1""",
                (language, Status.FAILED),
            ).fetchone()
        return row["hub"] if row else None

    def get_last_post_intent(self, language: str = "en") -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                """SELECT intent FROM blog_posts WHERE intent IS NOT NULL AND language = ? AND status != ?
                   ORDER BY created_at DESC LIMIT 1""",
                (language, Status.FAILED),
            ).fetchone()
        return row["intent"] if row else None

    def get_category_related_posts(self, category_topic: str, exclude_wp_id: int | None = None, limit: int = 5) -> list[dict]:
        """같은 카테고리(topic 컬럼) 내 발행글 — 허브가 달라도 같은 그룹이면 연결 (최신순)."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT keyword, title, wp_url FROM blog_posts
                   WHERE status = ? AND wp_url IS NOT NULL AND topic = ? AND wp_post_id != ?
                   ORDER BY created_at DESC LIMIT ?""",
                (Status.PUBLISHED, category_topic, exclude_wp_id or 0, limit),
            ).fetchall()
        posts = [dict(r) for r in rows]
        return [p for p in posts if p.get("wp_url") and p.get("title")]

    def get_diversity_data(
        self,
        days: int = 60,
        platform: str | None = None,
        language: str | None = None,
        limit: int = 200,
    ) -> dict:
        """최근 발행 이력 조회. platform/language를 지정하면 동일 파이프라인 글만 대상으로 함."""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        conditions = ["created_at > ?", "status != ?"]
        params: list = [cutoff, Status.FAILED]
        if platform:
            conditions.append("platform = ?")
            params.append(platform)
        if language:
            conditions.append("language = ?")
            params.append(language)
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(
                f"""SELECT title, keyword, topic, created_at FROM blog_posts
                   WHERE {" AND ".join(conditions)}
                   ORDER BY created_at DESC LIMIT ?""",
                params,
            ).fetchall()
        posts = [dict(r) for r in rows]
        return {
            "titles":      [p["title"]      for p in posts],
            "keywords":    [p["keyword"]    for p in posts],
            "categories":  [p["topic"]      for p in posts],
            "created_at":  [p["created_at"] for p in posts],
        }

    # ── 조회수 ────────────────────────────────────────────────────────────────

    def increment_view_count(self, wp_post_id: int) -> int:
        with self._conn() as conn:
            conn.execute(
                "UPDATE blog_posts SET view_count = view_count + 1 WHERE wp_post_id = ?",
                (wp_post_id,),
            )
            conn.commit()
            row = conn.execute(
                "SELECT view_count FROM blog_posts WHERE wp_post_id = ?", (wp_post_id,)
            ).fetchone()
        return row["view_count"] if row else 0

    # ── 발행 문서 중복 방지 ───────────────────────────────────────────────────

    def get_published_keywords_for_hash(self, doc_hash: str) -> set[str]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT keyword FROM published_documents WHERE doc_hash = ?",
                (doc_hash,),
            ).fetchall()
        return {row["keyword"] for row in rows}

    def save_published_document(self, doc_hash: str, keyword: str, wp_post_id: int, wp_url: str) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO published_documents (doc_hash, keyword, wp_post_id, wp_url)
                   VALUES (?, ?, ?, ?)""",
                (doc_hash, keyword, wp_post_id, wp_url),
            )
            conn.commit()

    # ── 통계 ─────────────────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        with self._conn() as conn:
            total     = conn.execute("SELECT COUNT(*) as n FROM blog_posts").fetchone()["n"]
            published = conn.execute(
                "SELECT COUNT(*) as n FROM blog_posts WHERE status=?", (Status.PUBLISHED,)
            ).fetchone()["n"]
            total_views = conn.execute(
                "SELECT COALESCE(SUM(view_count),0) as n FROM blog_posts"
            ).fetchone()["n"]

            daily = conn.execute("""
                SELECT substr(created_at,1,10) as date, COUNT(*) as count
                FROM blog_posts
                WHERE created_at >= date('now','-30 days')
                GROUP BY date ORDER BY date
            """).fetchall()

            by_topic = conn.execute("""
                SELECT topic, COALESCE(SUM(view_count),0) as views, COUNT(*) as posts
                FROM blog_posts WHERE status=?
                GROUP BY topic ORDER BY views DESC
            """, (Status.PUBLISHED,)).fetchall()

            top_posts = conn.execute("""
                SELECT title, wp_url, view_count, topic, substr(created_at,1,10) as date
                FROM blog_posts WHERE status=? AND view_count > 0
                ORDER BY view_count DESC LIMIT 10
            """, (Status.PUBLISHED,)).fetchall()

        return {
            "total":       total,
            "published":   published,
            "total_views": int(total_views),
            "daily":    [{"date": r["date"],  "count": r["count"]} for r in daily],
            "by_topic": [{"topic": r["topic"] or "기타", "views": int(r["views"]), "posts": r["posts"]} for r in by_topic],
            "top_posts":[{"title": r["title"], "url": r["wp_url"], "views": r["view_count"],
                          "topic": r["topic"], "date": r["date"]} for r in top_posts],
        }
