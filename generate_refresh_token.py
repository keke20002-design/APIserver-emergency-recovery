"""Google Ads / AdMob / GA4 Data API용 OAuth Refresh Token 1회성 발급 스크립트.

사전 준비 (한 번만):
1. Google Cloud Console > API 및 서비스 > OAuth 동의 화면 에서
   본인 Google 계정을 "테스트 사용자"로 추가해야 합니다.
   (동의 화면이 검증되기 전이라 테스트 사용자만 로그인 가능)
2. .env에 GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET이 채워져 있어야 합니다.

실행:
    python generate_refresh_token.py

브라우저가 열리면 Google Ads / AdMob / Analytics 데이터를 읽을 계정으로 로그인하고
동의하세요. 발급된 refresh token은 .env의 GOOGLE_REFRESH_TOKEN에 자동으로 저장됩니다.
"""
from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow

ENV_PATH = Path(__file__).parent / ".env"

# Google Ads API + AdMob API + GA4 Data API 접근에 필요한 최소 scope
SCOPES = [
    "https://www.googleapis.com/auth/adwords",
    "https://www.googleapis.com/auth/admob.readonly",
    "https://www.googleapis.com/auth/admob.report",
    "https://www.googleapis.com/auth/analytics.readonly",
]


def load_env_vars() -> tuple[str, str]:
    load_dotenv(ENV_PATH)
    import os

    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
    if not client_id or not client_secret:
        sys.exit(
            "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET이 .env에 없습니다. "
            "먼저 Google Cloud Console에서 OAuth 클라이언트(데스크톱 앱)를 만들고 .env에 채워주세요."
        )
    return client_id, client_secret


def run_oauth_flow(client_id: str, client_secret: str) -> str:
    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    flow = InstalledAppFlow.from_client_config(client_config, scopes=SCOPES)
    # access_type=offline + prompt=consent : 이미 한 번 승인한 계정이어도 반드시 refresh_token을 새로 받는다.
    credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    if not credentials.refresh_token:
        sys.exit(
            "refresh_token을 받지 못했습니다. 이미 이 앱에 동의한 계정이면 "
            "Google 계정 설정 > 보안 > 타사 앱 액세스에서 이 앱 연결을 해제한 뒤 다시 시도하세요."
        )
    return credentials.refresh_token


def save_refresh_token(refresh_token: str) -> None:
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    updated = False
    for i, line in enumerate(lines):
        if line.startswith("GOOGLE_REFRESH_TOKEN="):
            lines[i] = f"GOOGLE_REFRESH_TOKEN={refresh_token}"
            updated = True
            break
    if not updated:
        lines.append(f"GOOGLE_REFRESH_TOKEN={refresh_token}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    print("사전 확인: Google Cloud Console > OAuth 동의 화면 > 테스트 사용자에 로그인할 계정을 추가하셨나요?")
    print("브라우저가 곧 열립니다. Google Ads / AdMob / GA4 데이터를 관리하는 계정으로 로그인하세요.\n")

    client_id, client_secret = load_env_vars()
    refresh_token = run_oauth_flow(client_id, client_secret)
    save_refresh_token(refresh_token)

    print("\n완료! .env의 GOOGLE_REFRESH_TOKEN에 저장했습니다.")
    print("다음 단계: GOOGLE_ADS_DEVELOPER_TOKEN, GOOGLE_ADS_CUSTOMER_ID, ADMOB_PUBLISHER_ID, GA4_PROPERTY_ID를")
    print(".env에 채운 뒤 APIserver를 재시작하세요.")


if __name__ == "__main__":
    main()
