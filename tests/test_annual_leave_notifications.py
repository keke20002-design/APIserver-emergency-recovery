from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from fastapi import HTTPException
import pytest

from services import annual_leave_router as api


class Document:
    def __init__(self, key, data):
        self.id, self.data, self.reference = key, data, self

    def get(self, **kwargs):
        return self

    def to_dict(self):
        return dict(self.data)

    def update(self, values):
        self.data.update(values)

    def delete(self):
        self.data = {}


class Collection:
    def __init__(self, documents):
        self.documents = documents

    def where(self, field, operator, value):
        return Collection([d for d in self.documents if d.data.get(field) == value])

    def limit(self, count):
        return Collection(self.documents[:count])

    def stream(self):
        return iter(self.documents)


@pytest.fixture
def outbox(monkeypatch):
    event = Document("event", {"uid": "teacher", "companyId": "company", "requestId": "request", "title": "연차", "body": "확인", "sent": False})
    devices = [Document("device1", {"uid": "teacher", "token": "one"}), Document("device2", {"uid": "teacher", "token": "two"})]
    db = Mock()
    db.collection.side_effect = lambda name: Collection([event] if name == "annual_leave_outbox" else devices)
    transaction = Mock()
    transaction.update.side_effect = lambda ref, data: ref.update(data)
    db.transaction.return_value = transaction
    monkeypatch.setattr(api, "configured", lambda: True)
    monkeypatch.setattr(api, "database", lambda: db)
    monkeypatch.setattr(api, "firebase_app", lambda: object())
    monkeypatch.setattr(api.firestore, "transactional", lambda function: function)
    return event, devices


def test_successful_devices_are_not_retried(outbox, monkeypatch):
    event, devices = outbox
    attempted = []
    def send(message, **kwargs):
        attempted.append(message.token)
        if message.token == "two" and attempted.count("two") == 1:
            raise RuntimeError("temporary delivery failure")
    monkeypatch.setattr(api.messaging, "send", send)
    api.deliver_notifications()
    assert not event.data["sent"]
    assert event.data["deliveredDevices"] == ["device1"]
    api.deliver_notifications()
    assert event.data["sent"]
    assert attempted == ["one", "two", "two"]


def test_active_delivery_lease_skips_concurrent_worker(outbox, monkeypatch):
    event, _ = outbox
    event.data["leaseUntil"] = datetime.now(timezone.utc) + timedelta(minutes=1)
    send = Mock()
    monkeypatch.setattr(api.messaging, "send", send)
    api.deliver_notifications()
    send.assert_not_called()


def test_missing_bearer_token_is_unauthorized(monkeypatch):
    monkeypatch.setattr(api, "firebase_app", lambda: object())
    with pytest.raises(HTTPException) as error:
        api.current_user(authorization="")
    assert error.value.status_code == 401
