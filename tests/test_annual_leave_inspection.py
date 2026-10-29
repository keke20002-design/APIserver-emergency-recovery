from datetime import date

import pytest

from services.annual_leave_domain import LeaveError, balance, migrate, mutate, new_company, visible

TODAY = date(2026, 9, 28)
NOW = "2026-09-28T09:14:00+09:00"


@pytest.fixture
def company():
    c = new_company("햇살 어린이집", "owner", "홍길동", TODAY)
    for uid in ("teacher", "other"):
        c["members"][uid] = {"name": uid, "role": "TEACHER", "classGroup": "햇살반",
            "joinedAt": "2025-01-01", "attendance": {},
            "grants": [{"id": "opening", "units": 60, "spent": 0, "date": "2026-01-01", "expires": "2027-01-01"}]}
    c["schema"] = 2
    return c


def request(c, request_id="req-1", **changes):
    p = {"leaveType": "FULL", "startDate": "2026-10-05", "endDate": "2026-10-05", **changes}
    mutate(c, "teacher", "request", p, TODAY, request_id)
    return c["requests"][request_id]


def approve(c, request_id="req-1", **payload):
    return mutate(c, "owner", "approve", {"requestId": request_id, **payload}, TODAY, f"approve-{request_id}", now=NOW)


def issues(c, request_id="req-1", today=TODAY):
    return next(r for r in visible(c, "owner", today)["requests"] if r["id"] == request_id)["issues"]


def used(c):
    return balance(c["members"]["teacher"], c["requests"], "teacher", TODAY)["used"]


def test_approval_records_approver_and_time(company):
    request(company)
    approve(company)
    r = company["requests"]["req-1"]
    assert (r["approvedBy"], r["approvedByName"], r["approvedAt"]) == ("owner", "홍길동", NOW)


def test_approve_cancel_cycles_never_double_deduct(company):
    for i in range(3):
        request(company, f"r{i}")
        approve(company, f"r{i}")
        with pytest.raises(LeaveError):
            approve(company, f"r{i}")  # repeated tap / retried request with a new id
        assert used(company) == 4
        assert company["members"]["teacher"]["grants"][0]["spent"] == 4
        mutate(company, "teacher", "cancel", {"requestId": f"r{i}"}, TODAY, f"c{i}")
        mutate(company, "owner", "approveCancel", {"requestId": f"r{i}"}, TODAY, f"ac{i}", now=NOW)
        with pytest.raises(LeaveError):
            mutate(company, "owner", "approveCancel", {"requestId": f"r{i}"}, TODAY, f"ac{i}-again")
        assert used(company) == 0
        assert company["members"]["teacher"]["grants"][0]["spent"] == 0
    assert company["requests"]["r0"]["cancelledByName"] == "홍길동"


def test_half_day_is_half_unit_and_needs_coverage_record(company):
    request(company, leaveType="HALF_AM")
    approve(company)
    assert company["requests"]["req-1"]["units"] == 2
    assert company["members"]["teacher"]["grants"][0]["spent"] == 2
    assert issues(company) == ["보육공백 처리 확인 필요"]


@pytest.mark.parametrize("coverage", [
    {"method": "SUBSTITUTE", "substituteName": "이대체", "startTime": "09:00", "endTime": "18:00"},
    {"method": "INTERNAL", "staffUid": "other", "detail": "합반 운영"},
    {"method": "NONE"},
    {"method": "OTHER", "detail": "오후 행사로 결원 없음"},
])
def test_any_recorded_coverage_method_completes_the_record(company, coverage):
    request(company)
    approve(company)
    assert "보육공백 처리 확인 필요" in issues(company)
    mutate(company, "owner", "coverage", {"requestId": "req-1", "coverage": coverage}, TODAY, "cov", now=NOW)
    assert issues(company) == []


def test_coverage_input_is_validated(company):
    request(company)
    with pytest.raises(LeaveError, match="승인된 휴가"):
        mutate(company, "owner", "coverage", {"requestId": "req-1", "coverage": {"method": "NONE"}}, TODAY, "x")
    approve(company)
    for bad in ({"method": "OTHER"}, {"method": "SUBSTITUTE"}, {"method": "INTERNAL"}, {"method": "?"},
                {"method": "SUBSTITUTE", "substituteName": "a", "startTime": "18:00", "endTime": "09:00"},
                {"method": "SUBSTITUTE", "substituteName": "a", "startTime": "9시"},
                {"method": "INTERNAL", "staffUid": "teacher"}):
        with pytest.raises(LeaveError):
            mutate(company, "owner", "coverage", {"requestId": "req-1", "coverage": bad}, TODAY, "x")
    with pytest.raises(LeaveError):
        mutate(company, "teacher", "coverage", {"requestId": "req-1", "coverage": {"method": "NONE"}}, TODAY, "x")


def test_coverage_edit_keeps_before_after_history(company):
    request(company)
    approve(company)
    first = {"method": "SUBSTITUTE", "substituteName": "이대체", "startTime": "09:00", "endTime": "17:00"}
    mutate(company, "owner", "coverage", {"requestId": "req-1", "coverage": first}, TODAY, "c1", now=NOW)
    mutate(company, "owner", "coverage", {"requestId": "req-1", "coverage": {**first, "endTime": "18:00"}},
           TODAY, "c2", now=NOW)
    # Same content again is a no-op, not a new history line.
    mutate(company, "owner", "coverage", {"requestId": "req-1", "coverage": {**first, "endTime": "18:00"}},
           TODAY, "c3", now=NOW)
    entries = [e for e in company["audit"] if e["target"] == "req-1"]
    assert [e["action"] for e in entries] == ["request", "approve", "coverage", "coverage"]
    last = entries[-1]
    assert last["before"]["coverage"]["endTime"] == "17:00"
    assert last["after"]["coverage"]["endTime"] == "18:00"
    assert (last["actor"], last["actorName"], last["at"], last["targetType"]) == ("owner", "홍길동", NOW, "request")
    assert company["requests"]["req-1"]["substituteName"] == "이대체"


def test_internal_staff_is_notified_once(company):
    request(company)
    approve(company)
    cov = {"method": "INTERNAL", "staffUid": "other", "detail": "합반"}
    events = mutate(company, "owner", "coverage", {"requestId": "req-1", "coverage": cov}, TODAY, "c1")
    assert [e[0] for e in events] == ["other"]
    events = mutate(company, "owner", "coverage", {"requestId": "req-1", "coverage": {**cov, "detail": "합반 운영"}},
                    TODAY, "c2")
    assert events == []


def test_approve_can_record_coverage_in_one_step(company):
    request(company)
    approve(company, coverage={"method": "NONE"})
    assert issues(company) == []


def test_old_app_substitute_fields_still_record_coverage(company):
    request(company)
    approve(company, substituteName="외부 교사", substituteOrganization="대체인력뱅크")
    r = company["requests"]["req-1"]
    assert r["coverage"]["method"] == "SUBSTITUTE"
    assert r["substituteOrganization"] == "대체인력뱅크"
    assert issues(company) == []


def test_past_pending_and_rejected_requests(company):
    request(company)
    assert issues(company) == []
    assert issues(company, today=date(2026, 10, 6)) == ["휴가일이 지났지만 승인 처리 전"]
    mutate(company, "owner", "reject", {"requestId": "req-1", "note": "일정"}, TODAY, "rej", now=NOW)
    assert issues(company, today=date(2026, 10, 6)) == []
    assert company["requests"]["req-1"]["reviewedByName"] == "홍길동"


def test_detects_missing_approver_and_unlinked_usage(company):
    request(company)
    approve(company, coverage={"method": "NONE"})
    r = company["requests"]["req-1"]
    r.pop("approvedBy")
    r.pop("approvedAt")
    r["allocations"] = []
    assert issues(company) == ["승인자 기록 없음", "승인일시 기록 없음", "연차 사용내역 연결 확인 필요"]


def test_teacher_view_has_no_inspection_data(company):
    request(company)
    approve(company)
    mine = visible(company, "teacher", TODAY)["requests"][0]
    assert "issues" not in mine and "history" not in mine


def test_legacy_data_is_backfilled_once(company):
    company.pop("schema")
    company["requests"]["old"] = {"id": "old", "userId": "teacher", "userName": "teacher", "classGroup": "햇살반",
        "leaveType": "FULL", "units": 4, "dates": ["2026-09-01"], "startDate": "2026-09-01",
        "endDate": "2026-09-01", "startMinute": 0, "endMinute": 1440, "status": "APPROVED",
        "allocations": [{"grantId": "opening", "units": 4}], "substituteName": "김대체", "substituteUid": "",
        "substituteOrganization": "", "createdAt": "2026-08-20"}
    company["audit"].append({"id": "a", "actor": "owner", "action": "approve", "date": "2026-08-21",
                             "note": "", "target": "old", "units": None})
    assert migrate(company) is True
    r = company["requests"]["old"]
    assert (r["approvedByName"], r["approvedAt"]) == ("홍길동", "2026-08-21")
    assert r["coverage"]["method"] == "SUBSTITUTE" and r["coverage"]["legacy"]
    assert issues(company, "old") == []
    assert migrate(company) is False
