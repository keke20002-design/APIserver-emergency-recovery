import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_RAW_FILE = Path(__file__).parent.parent / "data" / "raw_updates.json"


def save_raw(items: list[dict]) -> None:
    """수집된 raw 검색 결과를 로컬 파일에 적재."""
    _RAW_FILE.parent.mkdir(parents=True, exist_ok=True)
    existing = load_raw()
    existing.extend(items)
    with open(_RAW_FILE, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    logger.info("Raw store: saved %d items (total %d)", len(items), len(existing))


def load_raw() -> list[dict]:
    """저장된 raw 항목 전체 반환."""
    if not _RAW_FILE.exists():
        return []
    try:
        with open(_RAW_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error("Failed to load raw store: %s", e)
        return []


def clear_raw() -> None:
    """처리 완료 후 raw 파일 초기화."""
    if _RAW_FILE.exists():
        _RAW_FILE.write_text("[]", encoding="utf-8")
    logger.info("Raw store cleared")
