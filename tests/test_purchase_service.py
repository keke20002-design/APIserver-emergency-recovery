from unittest.mock import MagicMock

import pytest

from services import purchase_service


class _FakeDocSnapshot:
    def __init__(self, data):
        self._data = data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return self._data


class _FakeDocRef:
    def __init__(self, store, key):
        self._store = store
        self._key = key

    def get(self, transaction=None):
        return _FakeDocSnapshot(self._store.get(self._key))


class _FakeTransaction:
    def set(self, ref, data, merge=False):
        if merge and ref._key in ref._store:
            ref._store[ref._key] = {**ref._store[ref._key], **data}
        else:
            ref._store[ref._key] = dict(data)


class _FakeCollection:
    def __init__(self, store):
        self._store = store

    def document(self, key):
        return _FakeDocRef(self._store, key)


class _FakeFirestoreClient:
    def __init__(self):
        self._collections: dict[str, dict] = {}

    def collection(self, name):
        return _FakeCollection(self._collections.setdefault(name, {}))

    def transaction(self):
        return _FakeTransaction()


@pytest.fixture(autouse=True)
def _no_retry_transactions(monkeypatch):
    # 실제 firestore.transactional은 재시도/커밋을 관리하는 진짜 Transaction
    # 객체를 요구한다 — 테스트에서는 재시도 없이 한 번만 실행하는 identity
    # 데코레이터로 대체해서 _FakeTransaction만으로 충분하게 만든다.
    monkeypatch.setattr(purchase_service.firestore, "transactional", lambda f: f)


@pytest.fixture
def fake_db(monkeypatch):
    db = _FakeFirestoreClient()
    monkeypatch.setattr(purchase_service, "_firestore_client", lambda: db)
    return db


def _fake_play_client(purchase_state=0):
    client = MagicMock()
    products = client.purchases.return_value.products.return_value
    products.get.return_value.execute.return_value = {"purchaseState": purchase_state}
    products.consume.return_value.execute.return_value = {}
    return client, products


# --- get_status ---


@pytest.mark.asyncio
async def test_get_status_defaults_when_no_doc(fake_db):
    status = await purchase_service.get_status("u1")
    assert status == {"aiCoachingBalance": 0, "removeAds": False}


@pytest.mark.asyncio
async def test_get_status_returns_stored_values(fake_db):
    fake_db._collections.setdefault("users", {})["u1"] = {
        "aiCoachingBalance": 7,
        "removeAds": True,
    }
    status = await purchase_service.get_status("u1")
    assert status == {"aiCoachingBalance": 7, "removeAds": True}


# --- consume_credit ---


@pytest.mark.asyncio
async def test_consume_credit_decrements_balance(fake_db):
    fake_db._collections.setdefault("users", {})["u1"] = {"aiCoachingBalance": 3}
    result = await purchase_service.consume_credit("u1")
    assert result["aiCoachingBalance"] == 2
    assert fake_db._collections["users"]["u1"]["aiCoachingBalance"] == 2


@pytest.mark.asyncio
async def test_consume_credit_fails_when_balance_zero(fake_db):
    with pytest.raises(ValueError):
        await purchase_service.consume_credit("u1")


# --- verify_and_grant_purchase ---


@pytest.mark.asyncio
async def test_grants_coaching_credits_for_consumable(fake_db, monkeypatch):
    client, products = _fake_play_client(purchase_state=0)
    monkeypatch.setattr(purchase_service, "_play_client", lambda: client)

    result = await purchase_service.verify_and_grant_purchase("u1", "ai_coaching_30", "tok-1")

    assert result["aiCoachingBalance"] == 30
    assert fake_db._collections["purchaseRecords"]["tok-1"]["processed"] is True
    products.consume.assert_called_once()


@pytest.mark.asyncio
async def test_grants_remove_ads_for_non_consumable(fake_db, monkeypatch):
    client, products = _fake_play_client(purchase_state=0)
    monkeypatch.setattr(purchase_service, "_play_client", lambda: client)

    result = await purchase_service.verify_and_grant_purchase("u1", "remove_ads", "tok-2")

    assert result["removeAds"] is True
    products.consume.assert_not_called()


@pytest.mark.asyncio
async def test_duplicate_purchase_token_is_not_regranted(fake_db, monkeypatch):
    client, products = _fake_play_client(purchase_state=0)
    monkeypatch.setattr(purchase_service, "_play_client", lambda: client)

    await purchase_service.verify_and_grant_purchase("u1", "ai_coaching_10", "tok-3")
    result = await purchase_service.verify_and_grant_purchase("u1", "ai_coaching_10", "tok-3")

    assert result["aiCoachingBalance"] == 10  # not 20 — second call did nothing
    products.get.assert_called_once()  # Play was only verified once


@pytest.mark.asyncio
async def test_unknown_product_id_rejected(fake_db, monkeypatch):
    monkeypatch.setattr(purchase_service, "_play_client", lambda: _fake_play_client()[0])
    with pytest.raises(ValueError):
        await purchase_service.verify_and_grant_purchase("u1", "not_a_real_product", "tok-4")


@pytest.mark.asyncio
async def test_unpurchased_state_rejected(fake_db, monkeypatch):
    client, _ = _fake_play_client(purchase_state=1)  # 1 = canceled
    monkeypatch.setattr(purchase_service, "_play_client", lambda: client)

    with pytest.raises(ValueError):
        await purchase_service.verify_and_grant_purchase("u1", "ai_coaching_10", "tok-5")

    assert "tok-5" not in fake_db._collections.get("purchaseRecords", {})
