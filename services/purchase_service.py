import asyncio
import json
import logging
import os
from datetime import datetime, timezone

import firebase_admin
from firebase_admin import auth as firebase_auth
from firebase_admin import credentials, firestore
from google.oauth2 import service_account
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

_ANDROID_PACKAGE_NAME = os.getenv("ANDROID_PACKAGE_NAME", "com.hustle.speech_trainer")

# 소비성(코칭권) 상품 ID → 지급할 크레딧 수. Flutter의
# lib/core/constants/product_ids.dart와 반드시 동일하게 유지한다.
_COACHING_CREDITS: dict[str, int] = {
    "ai_coaching_10": 10,
    "ai_coaching_30": 30,
}
_REMOVE_ADS_PRODUCT_ID = "remove_ads"

_firebase_app: firebase_admin.App | None = None


def _get_firebase_app() -> firebase_admin.App:
    global _firebase_app
    if _firebase_app is None:
        cred_json = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "")
        if not cred_json:
            raise RuntimeError("FIREBASE_SERVICE_ACCOUNT_JSON not configured on server")
        cred = credentials.Certificate(json.loads(cred_json))
        # 별도 이름의 app으로 등록 — 이 서버의 다른 기능이 나중에 다른 Firebase
        # 프로젝트를 쓰게 되더라도 default app과 충돌하지 않는다.
        _firebase_app = firebase_admin.initialize_app(cred, name="speech-purchase")
    return _firebase_app


def _firestore_client():
    return firestore.client(app=_get_firebase_app())


def _play_client():
    cred_json = os.getenv("PLAY_SERVICE_ACCOUNT_JSON", "")
    if not cred_json:
        raise RuntimeError("PLAY_SERVICE_ACCOUNT_JSON not configured on server")
    info = json.loads(cred_json)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/androidpublisher"]
    )
    # cache_discovery=False — 서버(멀티요청) 환경에서 discovery 파일 캐시가
    # oauth2client<4.0.0 전용이라는 경고/버그를 유발하는 걸 피한다.
    return build("androidpublisher", "v3", credentials=creds, cache_discovery=False)


async def verify_id_token(id_token: str) -> str:
    """Firebase ID 토큰을 검증해 uid를 반환한다. 클라이언트가 uid를 직접 보내는 경로를
    두지 않기 위한 핵심 보안 장치 — 잔액/구매는 전부 이 uid로만 조회/기록한다."""
    decoded = await asyncio.to_thread(
        firebase_auth.verify_id_token, id_token, app=_get_firebase_app()
    )
    return decoded["uid"]


def _default_status() -> dict:
    return {"aiCoachingBalance": 0, "removeAds": False}


def _status_from_doc(data: dict) -> dict:
    return {
        "aiCoachingBalance": int(data.get("aiCoachingBalance", 0)),
        "removeAds": bool(data.get("removeAds", False)),
    }


async def get_status(uid: str) -> dict:
    db = _firestore_client()
    doc = await asyncio.to_thread(lambda: db.collection("users").document(uid).get())
    if not doc.exists:
        return _default_status()
    return _status_from_doc(doc.to_dict() or {})


async def consume_credit(uid: str) -> dict:
    """AI 코칭권 1개를 원자적으로 차감한다. 잔액이 이미 0이면 실패한다 — 호출부
    (feedbackProvider)가 무료/광고 일일 한도를 먼저 소진했는지 확인한 뒤에만 불러야 한다."""
    db = _firestore_client()

    def _run() -> dict:
        user_ref = db.collection("users").document(uid)
        transaction = db.transaction()

        @firestore.transactional
        def _update(transaction) -> dict:
            snapshot = user_ref.get(transaction=transaction)
            data = snapshot.to_dict() or {} if snapshot.exists else {}
            balance = int(data.get("aiCoachingBalance", 0))
            if balance <= 0:
                raise ValueError("AI 코칭권이 부족합니다.")
            transaction.set(user_ref, {"aiCoachingBalance": balance - 1}, merge=True)
            return {"aiCoachingBalance": balance - 1, "removeAds": bool(data.get("removeAds", False))}

        return _update(transaction)

    return await asyncio.to_thread(_run)


async def verify_and_grant_purchase(uid: str, product_id: str, purchase_token: str) -> dict:
    """Google Play 구매를 검증하고, 정상 구매일 때만 Firestore에 권한/잔액을 지급한다.
    같은 purchase_token이 이미 처리된 경우(클라이언트 재시도, 중복 콜백 등) 재지급 없이
    현재 상태만 반환한다 — Google이 권장하는 purchaseToken 기반 중복 방지 방식."""
    if product_id not in _COACHING_CREDITS and product_id != _REMOVE_ADS_PRODUCT_ID:
        raise ValueError(f"알 수 없는 상품 ID: {product_id}")
    is_consumable = product_id in _COACHING_CREDITS

    db = _firestore_client()
    record_ref = db.collection("purchaseRecords").document(purchase_token)

    existing = await asyncio.to_thread(lambda: record_ref.get())
    if existing.exists:
        return await get_status(uid)

    play = await asyncio.to_thread(_play_client)
    purchase = await asyncio.to_thread(
        lambda: play.purchases()
        .products()
        .get(packageName=_ANDROID_PACKAGE_NAME, productId=product_id, token=purchase_token)
        .execute()
    )
    # purchaseState: 0=구매완료, 1=취소, 2=대기
    if purchase.get("purchaseState") != 0:
        raise ValueError("정상 처리되지 않은 구매입니다.")

    def _grant() -> dict:
        user_ref = db.collection("users").document(uid)
        transaction = db.transaction()

        @firestore.transactional
        def _update(transaction) -> dict:
            snapshot = user_ref.get(transaction=transaction)
            data = snapshot.to_dict() or {} if snapshot.exists else {}
            if is_consumable:
                data["aiCoachingBalance"] = int(data.get("aiCoachingBalance", 0)) + _COACHING_CREDITS[
                    product_id
                ]
            else:
                data["removeAds"] = True
            transaction.set(user_ref, data, merge=True)
            transaction.set(
                record_ref,
                {
                    "uid": uid,
                    "productId": product_id,
                    "processed": True,
                    "processedAt": datetime.now(timezone.utc).isoformat(),
                },
            )
            return _status_from_doc(data)

        return _update(transaction)

    result = await asyncio.to_thread(_grant)

    if is_consumable:
        try:
            await asyncio.to_thread(
                lambda: play.purchases()
                .products()
                .consume(packageName=_ANDROID_PACKAGE_NAME, productId=product_id, token=purchase_token)
                .execute()
            )
        except Exception as e:
            # 지급은 이미 끝났고 purchaseRecords에도 기록됐다 — Play 쪽 consume 실패는
            # 로그만 남긴다. 재시도해도 purchaseRecords에 걸려 재지급되지 않는다.
            logger.warning("Play consume 실패 (product=%s): %s", product_id, e)

    return result
