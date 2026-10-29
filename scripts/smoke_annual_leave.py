"""Opt-in real Firestore transaction smoke test; removes only its generated data.

Run from APIserver: python -m scripts.smoke_annual_leave
Auth is overridden for synthetic identities; this does NOT test Google login/FCM.
"""
from concurrent.futures import ThreadPoolExecutor
import asyncio
from datetime import timedelta
import uuid

from fastapi import FastAPI
import httpx
from services import annual_leave_router as api


def main():
    db = api.database()
    prefix = "smoke_" + uuid.uuid4().hex
    owner, teacher, outsider = (prefix + suffix for suffix in ("_owner", "_teacher", "_outsider"))
    company_ids = []
    clients = []
    api.deliver_notifications = lambda: None

    def client(uid):
        app = FastAPI()
        app.include_router(api.router)
        app.dependency_overrides[api.current_user] = lambda: {"uid": uid}
        class ASGIClient:
            def request(self, method, path, **kwargs):
                async def run():
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as transport:
                        return await transport.request(method, path, **kwargs)
                return asyncio.run(run())
            def get(self, path):
                return self.request("GET", path)
            def post(self, path, **kwargs):
                return self.request("POST", path, **kwargs)
            def close(self):
                pass
        instance = ASGIClient()
        clients.append(instance)
        return instance

    admin, staff, stranger = client(owner), client(teacher), client(outsider)

    def post(c, path, body, expected=200):
        response = c.post("/annual-leave" + path, json=body)
        assert response.status_code == expected, f"{path}: {response.status_code} {response.text}"
        return response.json()

    def command(c, cid, action, payload, request_id=None, expected=200):
        return post(c, f"/companies/{cid}/commands", {"id": request_id or uuid.uuid4().hex, "action": action, "payload": payload}, expected)

    try:
        for name in ("연차관리 자동검증 A", "연차관리 자동검증 B"):
            company_ids.append(post(admin, "/companies", {"name": name, "displayName": "검증 원장"})["id"])
        cid = company_ids[0]
        state = admin.get(f"/annual-leave/companies/{cid}").json()
        post(staff, "/join", {"inviteCode": state["inviteCode"], "name": "검증 교사", "classGroup": "검증반", "joinedAt": "2026-01-01"})
        command(admin, cid, "approveJoin", {"userId": teacher})
        command(admin, cid, "adjust", {"userId": teacher, "units": 4, "opening": True, "note": "자동검증"})
        assert stranger.get(f"/annual-leave/companies/{cid}").status_code == 403
        assert staff.get(f"/annual-leave/companies/{company_ids[1]}").status_code == 403
        start = api.today() + timedelta(days=14)
        while start.weekday() >= 5:
            start += timedelta(days=1)
        command(admin, cid, "settings", {"excludePublicHolidays": False})
        request_id = uuid.uuid4().hex
        payload = {"leaveType": "FULL", "startDate": start.isoformat(), "endDate": start.isoformat(), "reason": "PRIVATE"}
        command(staff, cid, "request", payload, request_id)
        command(staff, cid, "request", payload, request_id)  # lost-response replay
        snap = staff.get(f"/annual-leave/companies/{cid}").json()
        assert len(snap["requests"]) == 1
        rid = snap["requests"][0]["id"]
        command(staff, cid, "approve", {"requestId": rid}, expected=400)

        def approve(_):
            return admin.post(f"/annual-leave/companies/{cid}/commands", json={"id": uuid.uuid4().hex, "action": "approve", "payload": {"requestId": rid}}).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = sorted(pool.map(approve, range(2)))
        assert statuses == [200, 400], statuses
        snap = staff.get(f"/annual-leave/companies/{cid}").json()
        assert next(m for m in snap["members"] if m["uid"] == teacher)["balance"]["available"] == 0
        command(staff, cid, "cancel", {"requestId": rid})
        command(admin, cid, "approveCancel", {"requestId": rid})
        snap = staff.get(f"/annual-leave/companies/{cid}").json()
        assert next(m for m in snap["members"] if m["uid"] == teacher)["balance"]["available"] == 4
        print("PASS: two workplaces, invite/join, private access, opening balance, idempotent request, concurrent approvals, cancellation restoration")
    finally:
        for cid in company_ids:
            state_ref = db.collection("annual_leave_private").document(cid)
            state = state_ref.get().to_dict() or {}
            if state.get("inviteCode"):
                db.collection("annual_leave_invites").document(state["inviteCode"]).delete()
            for collection in ("annual_leave_outbox",):
                for doc in db.collection(collection).where("companyId", "==", cid).stream():
                    doc.reference.delete()
            for doc in db.collection("annual_leave_receipts").stream():
                if doc.id.startswith(cid + "_"):
                    doc.reference.delete()
            state_ref.delete()
            db.collection("annual_leave_companies").document(cid).delete()
        for uid in (owner, teacher, outsider):
            ref = db.collection("annual_leave_users").document(uid)
            for doc in ref.collection("notifications").stream():
                doc.reference.delete()
            ref.delete()
        for c in clients:
            c.close()
        print("Removed only synthetic smoke-test records")


if __name__ == "__main__":
    main()
