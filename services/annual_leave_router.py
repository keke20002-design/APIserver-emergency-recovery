"""Annual Leave API, isolated from the other Firebase apps on this server."""
import hashlib
import json
import logging
import os
import secrets
import threading
import uuid
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import firebase_admin
from firebase_admin import auth, credentials, firestore, messaging
from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from services.annual_leave_domain import LeaveError, accrue, day, migrate, mutate, new_company, require, visible

router = APIRouter(prefix="/annual-leave", tags=["Annual Leave"])
logger = logging.getLogger(__name__)
_lock = threading.Lock()
_default_credentials = Path(__file__).resolve().parents[1] / ".secrets" / "annual-leave-service-account.json"


@router.get("/health")
def health():
    return {"ok": True, "service": "annual-leave"}


@router.get("/account-deletion", response_class=HTMLResponse)
def account_deletion_page():
    return HTMLResponse("""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>보육쉼표 계정 및 데이터 삭제 요청</title>
<style>
body{font-family:system-ui,-apple-system,'Noto Sans KR',sans-serif;background:#f4f7f7;color:#17343a;margin:0;padding:24px}
main{max-width:620px;margin:32px auto;background:white;border-radius:20px;padding:28px;box-shadow:0 8px 32px #1232}
h1{font-size:24px}p{line-height:1.7;color:#43585c}.field{margin:18px 0}label{display:block;font-weight:600;margin-bottom:7px}
input{box-sizing:border-box;width:100%;padding:13px;border:1px solid #b9c8c9;border-radius:10px;font-size:16px}
button{background:#105260;color:white;border:0;border-radius:10px;padding:14px 20px;font-size:16px;font-weight:700;width:100%;margin-top:10px}
.trap{position:absolute;left:-10000px}small{color:#65777a}
</style></head><body><main>
<h1>보육쉼표 계정 및 데이터 삭제 요청</h1>
<p>계정 소유를 확인한 뒤 보육쉼표 계정과 연결된 개인 데이터를 삭제해 달라고 요청할 수 있습니다. 요청을 보내면 운영자가 본인 확인 후 처리합니다. 어린이집의 공동 신청·승인 기록은 다른 구성원의 기록과 법적 보관 필요 여부를 확인해 개인 식별 정보를 제거하거나 필요한 범위에서 분리 보관할 수 있습니다.</p>
<form method="post" action="/annual-leave/account-deletion">
<div class="field"><label for="name">이름</label><input id="name" name="name" maxlength="80" required autocomplete="name"></div>
<div class="field"><label for="email">보육쉼표에 로그인한 Google 이메일</label><input id="email" name="email" type="email" maxlength="254" required autocomplete="email"></div>
<div class="field"><label for="company">어린이집 이름</label><input id="company" name="company" maxlength="80" required></div>
<div class="trap"><label>Leave this field empty<input name="website" tabindex="-1" autocomplete="off"></label></div>
<button type="submit">삭제 요청 제출</button></form>
<p><small>요청 처리를 위해 입력한 이름, 이메일, 어린이집 이름을 사용합니다. 요청이 확인되기 전에는 계정이나 데이터가 자동 삭제되지 않습니다. 이미 앱을 삭제했어도 이 페이지에서 요청할 수 있습니다.</small></p>
</main></body></html>""")


@router.post("/account-deletion", response_class=HTMLResponse)
async def submit_account_deletion(request: Request):
    from urllib.parse import parse_qs
    values = parse_qs((await request.body()).decode("utf-8", errors="replace"))
    name = values.get("name", [""])[0].strip()
    email = values.get("email", [""])[0].strip().lower()
    company = values.get("company", [""])[0].strip()
    if values.get("website", [""])[0].strip():
        return HTMLResponse("요청을 접수할 수 없습니다.", status_code=400)
    if not (1 <= len(name) <= 80 and 3 <= len(email) <= 254 and
            "@" in email and 1 <= len(company) <= 80):
        return HTMLResponse("이름, 로그인 이메일, 어린이집 이름을 확인해 주세요.", status_code=400)
    try:
        request_id = uuid.uuid4().hex
        database().collection("annual_leave_deletion_requests").document(request_id).create({
            "name": name, "email": email, "company": company,
            "status": "PENDING", "submittedAt": firestore.SERVER_TIMESTAMP,
        })
    except Exception:
        logger.exception("Annual leave account deletion request could not be stored")
        return HTMLResponse("요청을 저장하지 못했습니다. 잠시 후 다시 시도해 주세요.", status_code=503)
    return HTMLResponse(f"""<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>삭제 요청 접수</title><body style="font-family:system-ui,'Noto Sans KR',sans-serif;background:#f4f7f7;padding:24px;color:#17343a">
<main style="max-width:560px;margin:32px auto;background:white;border-radius:20px;padding:28px"><h1>삭제 요청을 접수했습니다</h1>
<p>요청 번호: <strong>{request_id}</strong></p><p>운영자가 계정 소유를 확인한 뒤 요청을 처리합니다. 확인 전에는 데이터가 삭제되지 않습니다.</p>
<p>보육쉼표 앱 계정으로 사용한 Google 이메일에 접근할 수 있도록 유지해 주세요.</p></main></body></html>""")


def configured():
    return bool(os.getenv("ANNUAL_LEAVE_FIREBASE_SERVICE_ACCOUNT_JSON") or os.getenv("ANNUAL_LEAVE_FIREBASE_CREDENTIALS") or _default_credentials.is_file())


def firebase_app():
    with _lock:
        try:
            return firebase_admin.get_app("annual-leave")
        except ValueError:
            raw = os.getenv("ANNUAL_LEAVE_FIREBASE_SERVICE_ACCOUNT_JSON")
            path = os.getenv("ANNUAL_LEAVE_FIREBASE_CREDENTIALS") or (str(_default_credentials) if _default_credentials.is_file() else None)
            if not raw and not path:
                raise HTTPException(503, "연차관리 서버의 Firebase 인증 설정이 필요합니다.")
            cred = credentials.Certificate(json.loads(raw) if raw else path)
            require(cred.project_id == "hustle-annualleave", "연차관리 전용 Firebase 프로젝트 인증이 필요합니다.")
            return firebase_admin.initialize_app(cred, name="annual-leave")


def database():
    return firestore.client(app=firebase_app())


def current_user(authorization: str = Header(default="")):
    app = firebase_app()
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "로그인이 필요합니다.")
    try:
        return auth.verify_id_token(authorization[7:], app=app, check_revoked=True)
    except Exception:
        raise HTTPException(401, "로그인 정보가 만료되었습니다. 다시 로그인해 주세요.") from None


def today():
    return datetime.now(ZoneInfo("Asia/Seoul")).date()


def now_text():
    return datetime.now(ZoneInfo("Asia/Seoul")).isoformat(timespec="seconds")


class CompanyInput(BaseModel):
    requestId: str = Field(pattern=r"^[a-zA-Z0-9-]{8,64}$")
    name: str = Field(min_length=1, max_length=80)
    displayName: str = Field(min_length=1, max_length=40)


class JoinInput(BaseModel):
    inviteCode: str = Field(min_length=8, max_length=32)
    name: str = Field(min_length=1, max_length=40)
    classGroup: str = Field(min_length=1, max_length=40)
    joinedAt: str


class Command(BaseModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9-]{8,64}$")
    action: str = Field(max_length=30)
    payload: dict = Field(default_factory=dict)


class TokenInput(BaseModel):
    token: str = Field(min_length=20, max_length=4096)


def save_state(tx, db, cid, state):
    # Small workplaces: one transactional aggregate prevents cross-request races.
    # Fail explicitly before Firestore's 1 MiB document limit; never discard history.
    require(len(json.dumps(state, ensure_ascii=False).encode()) < 850000,
            "어린이집 기록 보관 용량에 도달했습니다. 서버 관리자에게 문의해 주세요.")
    state["revision"] += 1
    tx.set(db.collection("annual_leave_private").document(cid), state)
    tx.set(db.collection("annual_leave_companies").document(cid),
           {"name": state["name"], "revision": state["revision"]})


@router.get("/companies")
def companies(user=Depends(current_user)):
    db = database()
    profile = db.collection("annual_leave_users").document(user["uid"]).get().to_dict() or {}
    return {"companies": [{"id": cid, **info} for cid, info in profile.get("memberships", {}).items()]}


@router.post("/companies")
def create_company(body: CompanyInput, user=Depends(current_user)):
    db, uid, cid = database(), user["uid"], uuid.uuid4().hex
    if not body.name.strip() or not body.displayName.strip():
        raise HTTPException(400, "어린이집 이름과 원장님 이름을 입력해 주세요.")

    receipt = db.collection("annual_leave_company_creations").document(
        f"{uid}_{body.requestId}"
    )
    existing_receipt = receipt.get().to_dict()
    if existing_receipt is not None:
        return {"id": existing_receipt["companyId"]}

    # Keep creation attempts in Firestore so the limit also works when the API
    # runs on more than one process. Replays with the same request id are
    # handled above and do not consume another attempt.
    limit_ref = db.collection("annual_leave_company_creation_limits").document(uid)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)

    @firestore.transactional
    def reserve_attempt(tx):
        data = limit_ref.get(transaction=tx).to_dict() or {}
        attempts = [
            value for value in data.get("attempts", [])
            if isinstance(value, datetime) and value >= cutoff
        ]
        if len(attempts) >= 3:
            raise HTTPException(429, "어린이집 생성 요청이 너무 많습니다. 10분 후 다시 시도해 주세요.")
        attempts.append(datetime.now(timezone.utc))
        tx.set(limit_ref, {"attempts": attempts, "updatedAt": firestore.SERVER_TIMESTAMP})

    reserve_attempt(db.transaction())
    code = secrets.token_hex(6).upper()
    state = new_company(body.name.strip(), uid, body.displayName.strip(), today())
    state["inviteCode"] = code
    ref = db.collection("annual_leave_users").document(uid)

    @firestore.transactional
    def commit(tx):
        replay = receipt.get(transaction=tx).to_dict()
        if replay is not None:
            return replay["companyId"]
        profile = ref.get(transaction=tx).to_dict() or {"memberships": {}}
        owned = [
            company_id for company_id, membership in profile.get("memberships", {}).items()
            if membership.get("role") == "ADMIN"
        ]
        if len(owned) >= 3:
            raise HTTPException(
                409,
                "한 계정에서는 어린이집을 최대 3개까지 만들 수 있습니다.",
            )
        profile.setdefault("memberships", {})[cid] = {"name": state["name"], "role": "ADMIN"}
        tx.set(ref, profile)
        tx.create(db.collection("annual_leave_invites").document(code), {"companyId": cid})
        tx.create(receipt, {
            "companyId": cid,
            "uid": uid,
            "createdAt": firestore.SERVER_TIMESTAMP,
        })
        save_state(tx, db, cid, state)
        return cid
    created_id = commit(db.transaction())
    return {"id": created_id}


@router.post("/join")
def join(body: JoinInput, user=Depends(current_user)):
    db, uid = database(), user["uid"]
    try:
        require(bool(body.name.strip()) and bool(body.classGroup.strip()), "교사 이름과 담당 반을 입력해 주세요.")
        require(day(body.joinedAt) <= today(), "입사일은 오늘 이전으로 입력해 주세요.")
        invite = db.collection("annual_leave_invites").document(body.inviteCode.strip().upper()).get().to_dict()
        require(invite is not None, "초대 코드를 확인해 주세요.")
        cid = invite["companyId"]
        ref = db.collection("annual_leave_users").document(uid)

        @firestore.transactional
        def commit(tx):
            state = db.collection("annual_leave_private").document(cid).get(transaction=tx).to_dict()
            profile = ref.get(transaction=tx).to_dict() or {"memberships": {}}
            require(uid not in state["members"], "이미 소속된 어린이집입니다.")
            classes = state.get("settings", {}).get("classes", [])
            require(classes, "원장님이 반을 먼저 등록해야 합니다.")
            require(body.classGroup.strip() in classes, "등록된 반을 선택해 주세요.")
            state["pending"][uid] = {"name": body.name.strip(), "classGroup": body.classGroup.strip(), "joinedAt": body.joinedAt}
            profile.setdefault("memberships", {})[cid] = {"name": state["name"], "role": "PENDING"}
            tx.set(ref, profile)
            save_state(tx, db, cid, state)
        commit(db.transaction())
        return {"id": cid}
    except LeaveError as e:
        raise HTTPException(400, str(e)) from None


@router.get("/invites/{code}")
def invite_info(code: str, user=Depends(current_user)):
    db = database()
    try:
        invite = db.collection("annual_leave_invites").document(code.strip().upper()).get().to_dict()
        require(invite is not None, "초대 코드를 확인해 주세요.")
        state = db.collection("annual_leave_private").document(invite["companyId"]).get().to_dict()
        require(state is not None, "어린이집 정보를 찾을 수 없습니다.")
        return {"name": state["name"], "classes": state.get("settings", {}).get("classes", [])}
    except LeaveError as e:
        raise HTTPException(400, str(e)) from None


@router.get("/companies/{cid}")
def snapshot(cid: str, user=Depends(current_user)):
    db = database()
    try:
        @firestore.transactional
        def read(tx):
            state = db.collection("annual_leave_private").document(cid).get(transaction=tx).to_dict()
            require(state is not None and user["uid"] in state["members"], "가입 승인된 어린이집만 열람할 수 있습니다.")
            if migrate(state) | accrue(state, today()):
                save_state(tx, db, cid, state)
            return visible(state, user["uid"], today())
        return read(db.transaction())
    except LeaveError as e:
        raise HTTPException(403, str(e)) from None


@router.get("/companies/{cid}/audit")
def audit_log(cid: str, start: str, end: str, user=Depends(current_user)):
    """Full change history for a period — the snapshot only carries the latest entries."""
    try:
        first, last = day(start), day(end)
        require(first <= last, "기간을 확인해 주세요.")
        state = database().collection("annual_leave_private").document(cid).get().to_dict()
        require(state is not None and state["members"].get(user["uid"], {}).get("role") == "ADMIN",
                "관리자만 변경이력을 열람할 수 있습니다.")
        names = {uid: m["name"] for uid, m in state["members"].items()}
        entries = [{**e, "actorName": e.get("actorName") or names.get(e["actor"], "")}
                   for e in state.get("audit", []) if first.isoformat() <= e["date"] <= last.isoformat()]
        return {"audit": entries}
    except LeaveError as e:
        raise HTTPException(403, str(e)) from None


@router.post("/companies/{cid}/commands")
def command(cid: str, body: Command, background: BackgroundTasks, user=Depends(current_user)):
    db, uid = database(), user["uid"]
    try:
        require(len(json.dumps(body.payload).encode()) <= 16000, "입력 내용이 너무 깁니다.")
        public_holidays = set()
        if body.action == "request":
            import holidays
            start, end = day(body.payload.get("startDate")), day(body.payload.get("endDate"))
            require(0 <= (end - start).days <= 365, "신청 기간을 확인해 주세요.")
            public_holidays = {d.isoformat() for d in holidays.KR(years=range(start.year, end.year + 1))}

        @firestore.transactional
        def commit(tx):
            ref = db.collection("annual_leave_private").document(cid)
            state = ref.get(transaction=tx).to_dict()
            require(state is not None and uid in state["members"], "어린이집 접근 권한이 없습니다.")
            receipt = db.collection("annual_leave_receipts").document(f"{cid}_{uid}_{body.id}")
            previous = receipt.get(transaction=tx)
            fingerprint = hashlib.sha256(json.dumps(body.model_dump(), sort_keys=True).encode()).hexdigest()
            if previous.exists:
                require(previous.to_dict()["fingerprint"] == fingerprint, "같은 요청 번호로 다른 작업을 보낼 수 없습니다.")
                return
            events = []
            migrate(state)
            if body.action in ("approveJoin", "rejectJoin"):
                require(state["members"][uid]["role"] == "ADMIN", "관리자만 가입을 처리할 수 있습니다.")
                target = str(body.payload.get("userId", ""))
                pending = state["pending"].get(target)
                require(pending is not None, "가입 대기자를 찾을 수 없습니다.")
                profile_ref = db.collection("annual_leave_users").document(target)
                profile = profile_ref.get(transaction=tx).to_dict()
                if body.action == "approveJoin":
                    state["members"][target] = {**pending, "role": "TEACHER", "grants": [], "attendance": {}}
                    profile["memberships"][cid]["role"] = "TEACHER"
                else:
                    profile["memberships"].pop(cid, None)
                state["pending"].pop(target)
                tx.set(profile_ref, profile)
                events.append((target, "가입 처리 안내", "어린이집 가입 처리 결과를 확인해 주세요.", ""))
            else:
                if body.action not in ("adjust", "batchAdjust") or not body.payload.get("opening"):
                    accrue(state, today())
                membership_profiles = []
                if body.action == "profile":
                    # Read every member profile before transaction writes, then keep
                    # each user's multi-kindergarten switcher in sync with the canonical name.
                    profile_uids = set(state["members"]) | set(state["pending"])
                    membership_profiles = [
                        (member_uid, db.collection("annual_leave_users").document(member_uid),
                         db.collection("annual_leave_users").document(member_uid).get(transaction=tx).to_dict() or {})
                        for member_uid in profile_uids
                    ]
                events = mutate(state, uid, body.action, body.payload, today(), f"{uid}-{body.id}", public_holidays,
                                now_text())
                if body.action == "request":
                    state["requests"][f"{uid}-{body.id}"]["createdAt"] = datetime.now(timezone.utc).isoformat()
                if body.action == "profile":
                    for member_uid, profile_ref, profile in membership_profiles:
                        membership = profile.get("memberships", {}).get(cid)
                        if membership is not None:
                            membership["name"] = state["name"]
                            tx.set(profile_ref, profile)
            save_state(tx, db, cid, state)
            tx.create(receipt, {"fingerprint": fingerprint})
            for i, (target, title, message, request_id) in enumerate(events):
                event_id = f"{cid}_{uid}_{body.id}_{i}"
                event = {"uid": target, "companyId": cid, "requestId": request_id,
                         "title": title, "body": message, "createdAt": firestore.SERVER_TIMESTAMP,
                         "sent": False}
                tx.set(db.collection("annual_leave_outbox").document(event_id), event)
                tx.set(db.collection("annual_leave_users").document(target).collection("notifications").document(event_id),
                       {k: v for k, v in event.items() if k not in ("uid", "sent")})
        commit(db.transaction())
        background.add_task(deliver_notifications)
        return {"ok": True}
    except (LeaveError, ValueError, TypeError) as e:
        raise HTTPException(400, str(e)) from None


@router.post("/devices")
def register_device(body: TokenInput, user=Depends(current_user)):
    key = hashlib.sha256(body.token.encode()).hexdigest()
    database().collection("annual_leave_devices").document(key).set({"uid": user["uid"], "token": body.token})
    return {"ok": True}


@router.delete("/devices")
def unregister_device(body: TokenInput, user=Depends(current_user)):
    key = hashlib.sha256(body.token.encode()).hexdigest()
    ref = database().collection("annual_leave_devices").document(key)
    item = ref.get().to_dict()
    if item and item["uid"] == user["uid"]:
        ref.delete()
    return {"ok": True}


def deliver_notifications():
    """Durable outbox. Also invoked by the existing server scheduler every minute."""
    if not configured():
        return
    db = database()
    for doc in db.collection("annual_leave_outbox").where("sent", "==", False).limit(100).stream():
        @firestore.transactional
        def claim(tx):
            event = doc.reference.get(transaction=tx).to_dict()
            now = datetime.now(timezone.utc)
            if event.get("sent") or (event.get("leaseUntil") and event["leaseUntil"] > now):
                return None
            tx.update(doc.reference, {"leaseUntil": now + timedelta(minutes=5)})
            return event
        event = claim(db.transaction())
        if event is None:
            continue
        devices = list(db.collection("annual_leave_devices").where("uid", "==", event["uid"]).stream())
        success = True
        delivered = set(event.get("deliveredDevices", []))
        for device in devices:
            if device.id in delivered:
                continue
            try:
                messaging.send(messaging.Message(token=device.to_dict()["token"],
                    notification=messaging.Notification(title=event["title"], body=event["body"]),
                    data={"companyId": event["companyId"], "requestId": event["requestId"], "eventId": doc.id}), app=firebase_app())
                delivered.add(device.id)
                doc.reference.update({"deliveredDevices": list(delivered)})
            except messaging.UnregisteredError:
                device.reference.delete()
            except Exception:
                success = False
                logger.warning("Annual Leave push delivery failed; retained for retry")
        doc.reference.update({"sent": success, "leaseUntil": None})


def daily_accrual():
    if not configured():
        return
    db = database()
    for doc in db.collection("annual_leave_private").stream():
        @firestore.transactional
        def apply(tx):
            state = doc.reference.get(transaction=tx).to_dict()
            if accrue(state, today()):
                save_state(tx, db, doc.id, state)
        apply(db.transaction())
