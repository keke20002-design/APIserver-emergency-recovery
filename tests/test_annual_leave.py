from copy import deepcopy
from datetime import date

import pytest

from services.annual_leave_domain import (
    LeaveError, accrue, balance, mutate, new_company, visible,
)

TODAY = date(2026, 9, 28)


@pytest.fixture
def company():
    c = new_company("햇살 어린이집", "owner", "원장", TODAY)
    for uid in ("teacher", "other"):
        c["members"][uid] = {"name": uid, "role": "TEACHER", "classGroup": "햇살반",
            "joinedAt": "2025-01-01", "attendance": {},
            "grants": [{"id": "opening", "units": 60, "spent": 0, "date": "2026-01-01", "expires": "2027-01-01"}]}
    return c


def request(c, request_id="req-1", **changes):
    p = {"leaveType": "FULL", "startDate": "2026-10-05", "endDate": "2026-10-05", "reason": "private reason", **changes}
    return mutate(c, "teacher", "request", p, TODAY, request_id)


def test_approval_and_cancel_restore_exact_grant(company):
    request(company)
    assert balance(company["members"]["teacher"], company["requests"], "teacher", TODAY)["canRequest"] == 56
    mutate(company, "owner", "approve", {"requestId": "req-1"}, TODAY, "approve")
    assert company["members"]["teacher"]["grants"][0]["spent"] == 4
    with pytest.raises(LeaveError):
        mutate(company, "owner", "approve", {"requestId": "req-1"}, TODAY, "approve-2")
    mutate(company, "teacher", "cancel", {"requestId": "req-1"}, TODAY, "cancel")
    assert company["requests"]["req-1"]["status"] == "CANCEL_PENDING"
    mutate(company, "owner", "approveCancel", {"requestId": "req-1"}, TODAY, "cancel-approved")
    assert company["members"]["teacher"]["grants"][0]["spent"] == 0


def test_privacy_and_cross_user_approval(company):
    request(company)
    with pytest.raises(LeaveError):
        mutate(company, "other", "approve", {"requestId": "req-1"}, TODAY, "bad")
    mutate(company, "owner", "approve", {"requestId": "req-1"}, TODAY, "ok")
    response = visible(company, "other", TODAY)
    assert response["requests"] == []
    assert "reason" not in response["calendar"][0]
    assert "balance" not in next(m for m in response["members"] if m["uid"] == "teacher")
    assert response["inviteCode"] == ""
    with pytest.raises(LeaveError):
        visible(company, "outsider", TODAY)


def test_pending_reservations_prevent_overspend(company):
    company["members"]["teacher"]["grants"][0]["units"] = 4
    request(company)
    with pytest.raises(LeaveError, match="부족"):
        request(company, "req-2", startDate="2026-10-06", endDate="2026-10-06")


def test_overlap_and_half_days(company):
    request(company, leaveType="HALF_AM")
    request(company, "pm", leaveType="HALF_PM")
    with pytest.raises(LeaveError, match="겹칩니다"):
        request(company, "overlap", leaveType="QUARTER", startMinute=540)


def test_weekends_and_holidays_are_excluded(company):
    p = {"leaveType": "FULL", "startDate": "2026-10-02", "endDate": "2026-10-06"}
    mutate(company, "teacher", "request", p, TODAY, "range", {"2026-10-05"})
    assert company["requests"]["range"]["dates"] == ["2026-10-02", "2026-10-06"]
    assert company["requests"]["range"]["units"] == 8


def test_rejection_releases_reserved_days(company):
    request(company)
    with pytest.raises(LeaveError):
        mutate(company, "owner", "reject", {"requestId": "req-1"}, TODAY, "reject")
    mutate(company, "owner", "reject", {"requestId": "req-1", "note": "일정 협의"}, TODAY, "reject")
    assert balance(company["members"]["teacher"], company["requests"], "teacher", TODAY)["canRequest"] == 60


def test_expired_grant_not_revived_by_cancellation(company):
    request(company)
    mutate(company, "owner", "approve", {"requestId": "req-1"}, TODAY, "ok")
    mutate(company, "teacher", "cancel", {"requestId": "req-1"}, date(2027, 1, 2), "cancel")
    mutate(company, "owner", "approveCancel", {"requestId": "req-1"}, date(2027, 1, 2), "cancel-ok")
    assert balance(company["members"]["teacher"], company["requests"], "teacher", date(2027, 1, 2))["available"] == 0


def test_accrual_requires_attendance_and_is_idempotent(company):
    member = company["members"]["teacher"]
    member["grants"] = []
    assert not accrue(company, TODAY)
    member["attendance"] = {"year:1": 90}
    assert accrue(company, TODAY)
    assert member["grants"][0]["units"] == 60
    assert not accrue(company, TODAY)


def test_opening_balance_prevents_backfill(company):
    member = company["members"]["teacher"]
    member["grants"] = []
    mutate(company, "owner", "adjust", {"userId": "teacher", "units": 30, "opening": True, "note": "기존 잔액"}, TODAY, "opening-1")
    member["attendance"]["year:1"] = 100
    assert not accrue(company, TODAY)
    assert len(member["grants"]) == 1


def test_fiscal_proration_and_monthly_cap(company):
    member = company["members"]["teacher"]
    member.update(joinedAt="2025-07-01", grants=[], attendance={"year:1": 100, **{f"month:{i}": True for i in range(1, 12)}})
    company["settings"]["basis"] = "FISCAL_YEAR"
    accrue(company, TODAY)
    annual = next(g for g in member["grants"] if g["id"] == "annual:1")
    assert annual["units"] == 30
    assert len([g for g in member["grants"] if g["id"].startswith("monthly:")]) == 11


def test_accrual_seniority_cap(company):
    member = company["members"]["teacher"]
    member.update(joinedAt="2000-01-01", grants=[], attendance={"year:26": 100})
    accrue(company, TODAY)
    assert member["grants"][0]["units"] == 100


def test_low_attendance_uses_confirmed_months(company):
    member = company["members"]["teacher"]
    member.update(grants=[], attendance={"year:1": 70, "fullMonths:1": 7})
    accrue(company, TODAY)
    assert member["grants"][0]["units"] == 28


def test_low_attendance_waits_for_month_count(company):
    member = company["members"]["teacher"]
    member.update(grants=[], attendance={"year:1": 70})
    assert not accrue(company, TODAY)
    member["attendance"]["fullMonths:1"] = 5
    assert accrue(company, TODAY)
    assert member["grants"][0]["units"] == 20


def test_fiscal_boundary_in_same_calendar_year(company):
    member = company["members"]["teacher"]
    member.update(joinedAt="2026-01-01", grants=[], attendance={"year:1": 100})
    company["settings"].update(basis="FISCAL_YEAR", fiscalMonth=7)
    assert accrue(company, TODAY)
    assert member["grants"][0]["date"] == "2026-07-01"


def test_adjustment_cannot_spend_reserved_days(company):
    request(company)
    with pytest.raises(LeaveError):
        mutate(company, "owner", "adjust", {"userId": "teacher", "units": -60, "note": "정정"}, TODAY, "adjust")


def test_adjustment_note_is_optional(company):
    mutate(company, "owner", "adjust",
           {"userId": "teacher", "units": 4, "expires": "2026-12-31"},
           TODAY, "adjust-without-note")
    assert company["members"]["teacher"]["grants"][-1]["note"] == ""


def test_batch_adjustment_grants_every_selected_teacher(company):
    mutate(company, "owner", "batchAdjust", {
        "userIds": ["teacher", "other"],
        "units": 8,
        "expires": "2026-12-31",
    }, TODAY, "sunshine-batch")
    for uid in ("teacher", "other"):
        grant = company["members"][uid]["grants"][-1]
        assert grant["units"] == 8
        assert grant["expires"] == "2026-12-31"
        assert grant["id"].endswith(f":{uid}")


def test_resignation_requires_admin_processing_and_blocks_new_requests(company):
    mutate(company, "teacher", "resignationRequest", {}, TODAY, "resign-1")
    assert company["members"]["teacher"]["resignationRequested"] is True
    mutate(company, "owner", "approveResignation", {"userId": "teacher"}, TODAY, "resign-2")
    assert company["members"]["teacher"]["employmentStatus"] == "LEFT"
    with pytest.raises(LeaveError, match="퇴사 처리된"):
        request(company, "after-leaving")


def test_unassigned_substitute_does_not_block_approval(company):
    request(company, needsSubstitute=True)
    mutate(company, "owner", "approve", {"requestId": "req-1"}, TODAY, "ok")
    assert company["requests"]["req-1"]["status"] == "APPROVED"
    assert company["requests"]["req-1"]["substituteName"] == ""


def test_internal_substitute_receives_event(company):
    request(company, needsSubstitute=True)
    events = mutate(company, "owner", "approve", {"requestId": "req-1", "substituteUid": "other"}, TODAY, "ok")
    assert any(e[0] == "other" for e in events)


@pytest.mark.parametrize('change', [
    {"leaveType": "QUARTER", "startMinute": 1400},
    {"leaveType": "HALF_PM", "endDate": "2026-10-06"},
    {"startDate": "2026-09-01", "endDate": "2026-09-01"},
    {"startDate": "broken"},
])
def test_invalid_requests(company, change):
    with pytest.raises(LeaveError):
        request(company, **change)


def test_fiscal_policy_cannot_change_after_grant(company):
    company["members"]["teacher"]["grants"][0]["id"] = "annual:1"
    with pytest.raises(LeaveError):
        mutate(company, "owner", "settings", {"basis": "FISCAL_YEAR"}, TODAY, "policy")
