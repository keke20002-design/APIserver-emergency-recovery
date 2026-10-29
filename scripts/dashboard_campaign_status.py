"""Read-only local bridge for the dashboard when the shared API cannot restart."""
import json
import os
import sys
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv

load_dotenv(ROOT / ".env")
from services.google_ads_service import fetch_campaign_statuses

if __name__ == "__main__":
    rows = fetch_campaign_statuses(os.environ["GOOGLE_ADS_CUSTOMER_ID"])
    print(json.dumps({"campaigns": rows, "checked_at": datetime.now(timezone.utc).isoformat()}, ensure_ascii=True))
