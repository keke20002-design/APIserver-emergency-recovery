"""Kindergarten leave rules. All amounts are integer quarter-days.

Attendance is explicitly confirmed by an administrator; unknown attendance never
silently earns statutory leave. Fiscal proration is a configurable workplace rule.
"""
from calendar import monthrange
from datetime import date, timedelta
import math
import re


class LeaveError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise LeaveError(message)


def day(value):
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        raise LeaveError("날짜 형식을 확인해 주세요.") from None


def add_months(value, months):
    year, month = divmod(value.year * 12 + value.month - 1 + months, 12)
    return date(year, month + 1, min(value.day, monthrange(year, month + 1)[1]))


def new_company(name, uid, owner_name, today):
    return {
        "name": name, "ownerId": uid, "revision": 0,
        "settings": {"basis": "JOIN_DATE", "fiscalMonth": 1,
                     "quarterLeave": True, "workdays": [0, 1, 2, 3, 4],
                     "holidays": [], "excludePublicHolidays": True,
                     "carryover": False, "expiryMonths": 12, "classes": []},
        "members": {uid: {"name": owner_name, "role": "ADMIN", "employmentStatus": "ACTIVE", "classGroup": "원장",
                           "joinedAt": today.isoformat(), "grants": [], "attendance": {}}},
        "pending": {}, "requests": {}, "audit": [],
    }


def balance(member, requests, uid, today):
    used = sum(r["units"] for r in requests.values()
               if r["userId"] == uid and r["status"] in ("APPROVED", "CANCEL_PENDING"))
    reserved = sum(r["units"] for r in requests.values()
                   if r["userId"] == uid and r["status"] == "PENDING")
    # Allocation records stay attached to a grant, including expired grants.
    grants = member.get("grants", [])
    available = sum(max(0, g["units"] - g.get("spent", 0)) for g in grants
                    if g["date"] <= today.isoformat() and
                    (not g.get("expires") or g["expires"] > today.isoformat()))
    return {"available": available, "reserved": reserved, "used": used,
            "canRequest": max(0, available - reserved)}


def accrue(company, today):
    settings = company["settings"]
    changed = False
    for member in company["members"].values():
        if member["role"] == "ADMIN":
            continue
        joined = day(member["joinedAt"])
        grants = member.setdefault("grants", [])
        existing = {g["id"] for g in grants}
        attendance = member.get("attendance", {})
        candidates = []
        for month in range(1, 12):
            at = add_months(joined, month)
            if attendance.get(f"month:{month}") is True:
                candidates.append((f"monthly:{month}", at, 4, add_months(joined, 12)))
        years = today.year - joined.year
        for year in range(1, years + 2):
            at = add_months(joined, year * 12)
            if settings["basis"] == "FISCAL_YEAR":
                first = date(joined.year, settings["fiscalMonth"], 1)
                if first <= joined:
                    first = date(joined.year + 1, settings["fiscalMonth"], 1)
                at = date(first.year + year - 1, first.month, 1)
            percentage = attendance.get(f"year:{year}")
            if percentage is None:
                continue
            if percentage >= 80:
                completed = max(0, at.year - joined.year - ((at.month, at.day) < (joined.month, joined.day)))
                units = min(25, 15 + max(0, completed - 1) // 2) * 4
                if settings["basis"] == "FISCAL_YEAR" and year == 1 and at < add_months(joined, 12):
                    units = math.floor(60 * (at - joined).days / (add_months(joined, 12) - joined).days)
            else:
                if f"fullMonths:{year}" not in attendance:
                    continue
                units = int(attendance.get(f"fullMonths:{year}", 0)) * 4
            expiry = None if settings["carryover"] else add_months(at, settings["expiryMonths"])
            candidates.append((f"annual:{year}", at, units, expiry))
        for key, at, units, expiry in candidates:
            # Imported starting balance covers grants accrued up to its date.
            covered = member.get("openingAsOf")
            if key in existing or at > today or (covered and at.isoformat() <= covered):
                continue
            grants.append({"id": key, "date": at.isoformat(), "units": units, "spent": 0,
                           "expires": expiry.isoformat() if expiry else None, "note": "자동 부여"})
            changed = True
    return changed


def request_dates(payload, settings, public_holidays):
    start, end = day(payload.get("startDate")), day(payload.get("endDate"))
    require(0 <= (end - start).days <= 365, "신청 기간은 1~366일이어야 합니다.")
    kind = payload.get("leaveType")
    require(kind in ("FULL", "HALF_AM", "HALF_PM", "QUARTER"), "연차 종류를 확인해 주세요.")
    require(kind == "FULL" or start == end, "반차와 반반차는 하루씩 신청해 주세요.")
    require(kind != "QUARTER" or settings["quarterLeave"], "반반차가 허용되지 않은 어린이집입니다.")
    holidays = set(settings["holidays"]) | (public_holidays if settings["excludePublicHolidays"] else set())
    dates = [(start + timedelta(days=i)).isoformat() for i in range((end-start).days + 1)
             if (start + timedelta(days=i)).weekday() in settings["workdays"]
             and (start + timedelta(days=i)).isoformat() not in holidays]
    require(bool(dates), "선택한 기간에 근무일이 없습니다.")
    start_min, end_min = {"FULL": (0, 1440), "HALF_AM": (0, 780), "HALF_PM": (780, 1440),
                          "QUARTER": (int(payload.get("startMinute", 540)), int(payload.get("startMinute", 540)) + 120)}[kind]
    require(0 <= start_min < end_min <= 1440, "반반차 시간대를 확인해 주세요.")
    return dates, len(dates) * {"FULL": 4, "HALF_AM": 2, "HALF_PM": 2, "QUARTER": 1}[kind], start_min, end_min


def allocate(member, units, today, request_id):
    remaining, allocations = units, []
    for grant in sorted(member["grants"], key=lambda g: g.get("expires") or "9999"):
        if grant["date"] > today.isoformat() or (grant.get("expires") and grant["expires"] <= today.isoformat()):
            continue
        take = min(remaining, max(0, grant["units"] - grant.get("spent", 0)))
        if take:
            grant["spent"] = grant.get("spent", 0) + take
            allocations.append({"grantId": grant["id"], "units": take})
            remaining -= take
    require(remaining == 0, "사용 가능한 잔여 연차가 부족합니다.")
    return allocations


COVERAGE_METHODS = {"SUBSTITUTE": "대체교사 배치", "INTERNAL": "내부 대체근무",
                    "NONE": "보육공백 없음", "OTHER": "기타"}
_CLOCK = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
# Fields shown as before/after in the request change history.
_TRACKED = ("status", "approvedByName", "approvedAt", "coverage", "substituteName", "reviewNote")


def clean_coverage(company, r, c):
    require(isinstance(c, dict), "보육공백 처리 내용을 확인해 주세요.")
    method = c.get("method")
    require(method in COVERAGE_METHODS, "보육공백 처리 방식을 선택해 주세요.")

    def text(key, limit):
        return str(c.get(key) or "").strip()[:limit]

    def member_name(key):
        member_uid = text(key, 128)
        if not member_uid:
            return "", ""
        require(member_uid in company["members"] and member_uid != r["userId"], "교직원을 확인해 주세요.")
        return member_uid, company["members"][member_uid]["name"]

    out = {"method": method}
    if method == "SUBSTITUTE":
        sub_uid, name = member_name("substituteUid")
        name = name or text("substituteName", 40)
        require(bool(name), "대체교사 이름을 입력해 주세요.")
        start, end = text("startTime", 5), text("endTime", 5)
        require(all(not t or _CLOCK.match(t) for t in (start, end)), "근무 시간은 09:00 형식으로 입력해 주세요.")
        require(not (start and end) or start < end, "근무 종료시간이 시작시간보다 늦어야 합니다.")
        out.update(substituteUid=sub_uid, substituteName=name, startTime=start, endTime=end, memo=text("memo", 500))
    elif method == "INTERNAL":
        staff_uid, name = member_name("staffUid")
        name = name or text("staffName", 40)
        require(bool(name), "담당 교직원을 입력해 주세요.")
        out.update(staffUid=staff_uid, staffName=name, detail=text("detail", 500))
    elif method == "OTHER":
        detail = text("detail", 500)
        require(bool(detail), "처리 내용을 입력해 주세요.")
        out.update(detail=detail)
    else:
        out.update(memo=text("memo", 500))
    return out


def apply_coverage(company, r, coverage, actor, now):
    """Stores the coverage record. Returns None when nothing changed, otherwise the
    newly assigned staff uid to notify ('' when nobody new needs a notice)."""
    old = r.get("coverage") or {}
    previous = {k: v for k, v in old.items() if not k.startswith("recorded") and k != "legacy"}
    if previous == coverage:
        return None
    r["coverage"] = {**coverage, "recordedBy": actor, "recordedByName": company["members"][actor]["name"],
                     "recordedAt": now}
    staff = coverage.get("substituteUid") or coverage.get("staffUid") or ""
    # Keep the calendar/card fields in sync with the coverage record.
    r.update(substituteUid=staff, substituteName=coverage.get("substituteName") or coverage.get("staffName") or "")
    if coverage["method"] != "SUBSTITUTE":
        r["substituteOrganization"] = ""
    return staff if staff != (previous.get("substituteUid") or previous.get("staffUid") or "") else ""


def tracked(r):
    return {k: r.get(k) for k in _TRACKED if r.get(k) not in (None, "")} if r else None


def migrate(company):
    """Backfill approval metadata and substitute records made before inspection support."""
    if company.get("schema", 1) >= 2:
        return False
    approvals = {}
    for entry in company.get("audit", []):
        if entry.get("action") == "approve":
            approvals[entry.get("target")] = entry
    for r in company["requests"].values():
        if r.get("allocations") is not None and not r.get("approvedBy"):
            entry = approvals.get(r["id"])
            if entry:
                actor = company["members"].get(entry["actor"], {})
                r.update(approvedBy=entry["actor"], approvedByName=actor.get("name", ""), approvedAt=entry["date"])
        if r.get("substituteName") and not r.get("coverage"):
            r["coverage"] = {"method": "SUBSTITUTE", "substituteUid": r.get("substituteUid", ""),
                             "substituteName": r["substituteName"], "startTime": "", "endTime": "",
                             "memo": r.get("substituteOrganization", ""), "legacy": True,
                             "recordedBy": r.get("approvedBy", ""), "recordedByName": r.get("approvedByName", ""),
                             "recordedAt": r.get("approvedAt", "")}
    company["schema"] = 2
    return True


def request_issues(r, today, actions):
    """Record-keeping gaps for the inspection screen. Never a legal judgement."""
    status, issues = r.get("status"), []
    past = r.get("startDate", "") < today.isoformat()
    if status not in ("PENDING", "APPROVED", "REJECTED", "CANCEL_PENDING", "CANCELLED"):
        return ["승인 상태 기록 없음"]
    if status == "PENDING" and past:
        issues.append("휴가일이 지났지만 승인 처리 전")
    if status == "CANCEL_PENDING" and past:
        issues.append("취소 요청 처리 전")
    if status in ("APPROVED", "CANCEL_PENDING"):
        if not r.get("approvedBy"):
            issues.append("승인자 기록 없음")
        if not r.get("approvedAt"):
            issues.append("승인일시 기록 없음")
        if sum(a["units"] for a in r.get("allocations") or []) != r["units"]:
            issues.append("연차 사용내역 연결 확인 필요")
        if not (r.get("coverage") or {}).get("method"):
            issues.append("보육공백 처리 확인 필요")
        coverage = r.get("coverage") or {}
        if "approve" not in actions or (coverage.get("method") and not coverage.get("legacy")
                                        and not actions & {"approve", "coverage", "substitute"}):
            issues.append("변경이력 확인 필요")
    return issues


def mutate(company, uid, action, p, today, command_id, public_holidays=None, now=None):
    now = now or today.isoformat()
    member = company["members"].get(uid)
    require(member is not None, "어린이집 소속을 확인해 주세요.")
    admin = member["role"] == "ADMIN"
    requests = company["requests"]
    events = []
    before = tracked(requests.get(p.get("requestId"))) if isinstance(p.get("requestId"), str) else None
    if action == "request":
        require(member["role"] == "TEACHER", "교사 계정에서 신청해 주세요.")
        require(member.get("employmentStatus", "ACTIVE") == "ACTIVE", "퇴사 처리된 교사는 신청할 수 없습니다.")
        dates, units, start_min, end_min = request_dates(p, company["settings"], public_holidays or set())
        require(dates[0] >= today.isoformat(), "지난 날짜에는 신청할 수 없습니다.")
        require(units <= balance(member, requests, uid, today)["canRequest"], "신청 가능한 연차가 부족합니다.")
        for r in requests.values():
            if r["userId"] == uid and r["status"] in ("PENDING", "APPROVED", "CANCEL_PENDING"):
                require(not (set(dates) & set(r["dates"]) and start_min < r["endMinute"] and end_min > r["startMinute"]), "이미 신청한 시간과 겹칩니다.")
        requests[command_id] = {"id": command_id, "userId": uid, "userName": member["name"],
            "classGroup": member["classGroup"], "leaveType": p["leaveType"], "units": units,
            "dates": dates, "startDate": dates[0], "endDate": dates[-1], "startMinute": start_min,
            "endMinute": end_min, "reason": str(p.get("reason", ""))[:500],
            "needsSubstitute": bool(p.get("needsSubstitute")), "substituteName": "", "substituteUid": "",
            "substituteOrganization": "", "status": "PENDING", "createdAt": today.isoformat()}
        events.append((company["ownerId"], "새 연차 신청", f'{member["name"]} 교사의 신청을 확인해 주세요.', command_id))
    elif action in ("approve", "reject", "cancel", "approveCancel", "rejectCancel", "substitute"):
        r = requests.get(p.get("requestId"))
        require(r is not None, "신청을 찾을 수 없습니다.")
        target = company["members"][r["userId"]]
        if action == "cancel":
            require(r["userId"] == uid, "본인의 신청만 취소할 수 있습니다.")
            require(r["status"] in ("PENDING", "APPROVED"), "이미 처리된 신청입니다.")
            r["status"] = "CANCELLED" if r["status"] == "PENDING" else "CANCEL_PENDING"
            events.append((company["ownerId"], "연차 취소", f'{member["name"]} 교사의 취소 내역을 확인해 주세요.', r["id"]))
        else:
            require(admin, "관리자만 처리할 수 있습니다.")
            if action in ("approve", "reject"):
                require(r["status"] == "PENDING", "이미 처리된 신청입니다.")
                if action == "approve":
                    r["allocations"] = allocate(target, r["units"], today, r["id"])
                    r.update(status="APPROVED", approvedBy=uid, approvedByName=member["name"], approvedAt=now)
                else:
                    require(bool(str(p.get("note", "")).strip()), "반려 사유를 입력해 주세요.")
                    r.update(status="REJECTED", reviewedBy=uid, reviewedByName=member["name"], reviewedAt=now)
            elif action in ("approveCancel", "rejectCancel"):
                require(r["status"] == "CANCEL_PENDING", "취소 승인 대기 상태가 아닙니다.")
                if action == "approveCancel":
                    for allocation in r.get("allocations", []):
                        for grant in target["grants"]:
                            if grant["id"] == allocation["grantId"]:
                                grant["spent"] -= allocation["units"]
                    r.update(status="CANCELLED", cancelledBy=uid, cancelledByName=member["name"], cancelledAt=now)
                else:
                    require(bool(str(p.get("note", "")).strip()), "취소 반려 사유를 입력해 주세요.")
                    r["status"] = "APPROVED"
            else:
                require(r["status"] in ("APPROVED", "CANCEL_PENDING"), "승인된 신청에 배치할 수 있습니다.")
            if action in ("approve", "substitute"):
                if isinstance(p.get("coverage"), dict):
                    coverage = clean_coverage(company, r, p["coverage"])
                elif p.get("substituteUid") or str(p.get("substituteName", "")).strip():
                    # Older app versions send only the substitute fields.
                    coverage = clean_coverage(company, r, {"method": "SUBSTITUTE", **p,
                                                           "memo": p.get("substituteOrganization", "")})
                    r["substituteOrganization"] = str(p.get("substituteOrganization", ""))[:80]
                else:
                    coverage = None
                if coverage:
                    notify = apply_coverage(company, r, coverage, uid, now)
                    if notify:
                        events.append((notify, "대체교사 배치", f'{r["startDate"]} {r["classGroup"]} 배치를 확인해 주세요.', r["id"]))
            r["reviewNote"] = str(p.get("note", ""))[:500]
            events.append((r["userId"], "연차 처리 결과", f'{r["startDate"]} 신청의 처리 결과를 확인해 주세요.', r["id"]))
    elif action == "coverage":
        require(admin, "관리자만 보육공백 처리를 기록할 수 있습니다.")
        r = requests.get(p.get("requestId"))
        require(r is not None, "신청을 찾을 수 없습니다.")
        require(r["status"] in ("APPROVED", "CANCEL_PENDING"), "승인된 휴가에만 보육공백 처리를 기록할 수 있습니다.")
        notify = apply_coverage(company, r, clean_coverage(company, r, p.get("coverage")), uid, now)
        if notify is None:
            return []  # Same content resubmitted: nothing changed, nothing to log.
        if notify:
            events.append((notify, "대체교사 배치", f'{r["startDate"]} {r["classGroup"]} 배치를 확인해 주세요.', r["id"]))
    elif action == "resignationRequest":
        require(member["role"] == "TEACHER", "교사 계정에서 요청해 주세요.")
        require(member.get("employmentStatus", "ACTIVE") == "ACTIVE", "이미 퇴사 처리된 교사입니다.")
        active = [r for r in requests.values() if r["userId"] == uid and
                  r["status"] in ("PENDING", "APPROVED", "CANCEL_PENDING")]
        require(not active, "진행 중인 연차 신청을 먼저 처리해 주세요.")
        require(not member.get("resignationRequested"), "이미 퇴사 요청을 보냈습니다.")
        member["resignationRequested"] = True
        events.append((company["ownerId"], "퇴사 요청", f'{member["name"]} 교사의 퇴사 요청을 확인해 주세요.', ""))
    elif action in ("approveResignation", "rejectResignation"):
        require(admin, "관리자만 퇴사 처리를 할 수 있습니다.")
        target_id = p.get("userId")
        target = company["members"].get(target_id)
        require(target is not None and target["role"] == "TEACHER", "교사를 찾을 수 없습니다.")
        require(target.get("resignationRequested") is True, "대기 중인 퇴사 요청이 없습니다.")
        if action == "approveResignation":
            target["employmentStatus"] = "LEFT"
            target["leftAt"] = today.isoformat()
        target["resignationRequested"] = False
        events.append((target_id, "퇴사 처리 결과", "퇴사 요청 처리 결과를 확인해 주세요.", ""))
    elif action in ("adjust", "batchAdjust"):
        require(admin, "관리자만 조정할 수 있습니다.")
        target_ids = [p.get("userId")] if action == "adjust" else p.get("userIds")
        require(isinstance(target_ids, list) and 1 <= len(target_ids) <= 200,
                "조정할 교사를 선택해 주세요.")
        target_ids = list(dict.fromkeys(target_ids))
        require(all(isinstance(target_id, str)
                    and target_id in company["members"]
                    and company["members"][target_id]["role"] == "TEACHER"
                    for target_id in target_ids), "교사를 확인해 주세요.")
        units = p.get("units")
        require(type(units) is int and -4000 <= units <= 4000, "0.25일 단위로 입력해 주세요.")
        require(action != "batchAdjust" or not p.get("opening"),
                "최초 잔액은 교사별로 입력해 주세요.")
        expiry = p.get("expires") or None
        require(not expiry or day(expiry) > today, "만료일은 오늘 이후여야 합니다.")
        note = str(p.get("note", ""))[:500]
        for target_id in target_ids:
            target = company["members"][target_id]
            grant_id = command_id if action == "adjust" else f"{command_id}:{target_id}"
            if p.get("opening"):
                require(not target.get("openingAsOf") and not target["grants"], "초기 잔액이 이미 등록되어 있습니다. 증감 조정을 사용하세요.")
                require(units >= 0, "초기 잔액은 0 이상이어야 합니다.")
                target["openingAsOf"] = today.isoformat()
            if units < 0:
                require(-units <= balance(target, requests, target_id, today)["canRequest"], "대기 중인 신청을 제외한 잔액보다 많이 차감할 수 없습니다.")
                allocate(target, -units, today, grant_id)
            else:
                target["grants"].append({"id": grant_id, "date": today.isoformat(), "units": units,
                                         "spent": 0, "expires": expiry, "note": note})
    elif action == "attendance":
        require(admin, "관리자만 출근율을 확인할 수 있습니다.")
        target = company["members"].get(p.get("userId"))
        require(target is not None, "교사를 찾을 수 없습니다.")
        period, value = str(p.get("period", "")), p.get("value")
        prefix, _, suffix = period.partition(":")
        require(suffix.isdigit() and 1 <= int(suffix) <= 60, "확인할 근속 기간을 입력해 주세요.")
        require((prefix == "month" and int(suffix) <= 11 and type(value) is bool)
                or (prefix == "year" and type(value) in (int, float) and 0 <= value <= 100)
                or (prefix == "fullMonths" and type(value) is int and 0 <= value <= 12), "출근율 또는 개근 월수를 확인해 주세요.")
        grant_id = f'monthly:{suffix}' if prefix == "month" else f'annual:{suffix}'
        require(not any(g["id"] == grant_id for g in target["grants"]), "이미 부여한 기간입니다. 수정분은 연차 조정을 사용하세요.")
        target["attendance"][period] = value
        accrue(company, today)
    elif action == "profile":
        require(admin, "관리자만 어린이집 정보를 수정할 수 있습니다.")
        name = str(p.get("name", "")).strip()
        owner_name = str(p.get("ownerName", "")).strip()
        require(1 <= len(name) <= 80, "어린이집명은 1~80자로 입력해 주세요.")
        require(1 <= len(owner_name) <= 40, "원장명은 1~40자로 입력해 주세요.")
        company["name"] = name
        company["members"][company["ownerId"]]["name"] = owner_name
    elif action == "settings":
        require(admin, "관리자만 설정할 수 있습니다.")
        s = company["settings"]
        basis = p.get("basis", s["basis"])
        require(basis in ("JOIN_DATE", "FISCAL_YEAR"), "산정 기준을 확인해 주세요.")
        month = int(p.get("fiscalMonth", s["fiscalMonth"]))
        require(1 <= month <= 12, "회계연도 시작월을 확인해 주세요.")
        if basis != s["basis"] or month != s["fiscalMonth"]:
            require(not any(any(g["id"].startswith("annual:") for g in m["grants"]) for m in company["members"].values()), "자동 부여 이력이 있어 산정 기준 변경 전 정산이 필요합니다.")
        workdays = p.get("workdays", s["workdays"])
        require(isinstance(workdays, list) and workdays and all(type(d) is int and 0 <= d <= 6 for d in workdays), "근무 요일을 선택해 주세요.")
        holidays = p.get("holidays", s["holidays"])
        require(isinstance(holidays, list) and len(holidays) <= 366, "휴무일을 확인해 주세요.")
        for d in holidays:
            day(d)
        expiry = int(p.get("expiryMonths", s["expiryMonths"]))
        require(1 <= expiry <= 36, "소멸 기간은 1~36개월입니다.")
        classes = p.get("classes", s.get("classes", []))
        require(isinstance(classes, list) and len(classes) <= 100,
                "반 목록을 확인해 주세요.")
        classes = list(dict.fromkeys(str(name).strip() for name in classes if str(name).strip()))
        require(all(len(name) <= 40 for name in classes), "반 이름은 40자 이내로 입력해 주세요.")
        s.update(basis=basis, fiscalMonth=month, workdays=sorted(set(workdays)), holidays=holidays,
                 quarterLeave=bool(p.get("quarterLeave", s["quarterLeave"])),
                 carryover=bool(p.get("carryover", s["carryover"])), expiryMonths=expiry,
                 excludePublicHolidays=bool(p.get("excludePublicHolidays", s["excludePublicHolidays"])),
                 classes=classes)
    else:
        raise LeaveError("지원하지 않는 작업입니다.")
    if action == "request":
        target, target_type, after = command_id, "request", tracked(requests[command_id])
    elif isinstance(p.get("requestId"), str) and p["requestId"] in requests:
        target, target_type, after = p["requestId"], "request", tracked(requests[p["requestId"]])
    elif action in ("profile", "settings"):
        target, target_type, after = "", "company", None
    else:
        target, target_type, after = p.get("userId", p.get("userIds", "")), "member", None
    company["audit"].append({"id": command_id, "actor": uid, "actorName": member["name"], "action": action,
                             "date": today.isoformat(), "at": now, "note": str(p.get("note", ""))[:500],
                             "target": target, "targetType": target_type, "units": p.get("units"),
                             "before": before, "after": after})
    return events


def visible(company, uid, today):
    member = company["members"].get(uid)
    require(member is not None, "가입 승인을 기다려 주세요.")
    admin = member["role"] == "ADMIN"
    requests = company["requests"]
    own = [r for r in requests.values() if admin or r["userId"] == uid]
    if admin:
        history = {}
        for entry in company["audit"]:
            if isinstance(entry.get("target"), str) and entry["target"]:
                history.setdefault(entry["target"], []).append(entry)
        # Copies: the stored aggregate may still be serialized after this returns.
        own = [{**r, "history": history.get(r["id"], []),
                "issues": request_issues(r, today, {e["action"] for e in history.get(r["id"], [])})}
               for r in own]
    calendar = [{k: r[k] for k in ("id", "userId", "userName", "classGroup", "dates", "startDate", "endDate", "startMinute", "endMinute", "leaveType", "status", "substituteName", "substituteUid")}
                for r in requests.values() if r["status"] in ("APPROVED", "CANCEL_PENDING")]
    members = []
    for key, m in company["members"].items():
        item = {"uid": key, "name": m["name"], "classGroup": m["classGroup"], "role": m["role"],
                "employmentStatus": m.get("employmentStatus", "ACTIVE"),
                "resignationRequested": bool(m.get("resignationRequested"))}
        if admin or key == uid:
            item.update(m)
            item["balance"] = balance(m, requests, key, today)
        members.append(item)
    return {"name": company["name"], "role": member["role"], "uid": uid,
            "ownerName": company["members"][company["ownerId"]]["name"],
            "settings": company["settings"], "members": members, "requests": own, "calendar": calendar,
            "pending": [{"uid": key, **v} for key, v in company["pending"].items()] if admin else [],
            "inviteCode": company.get("inviteCode", "") if admin else "",
            "audit": company["audit"][-100:] if admin else []}
