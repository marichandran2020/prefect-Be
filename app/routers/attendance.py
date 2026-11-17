import re

from fastapi import APIRouter, HTTPException, status

from ..config import SITES
from ..database import get_db
from ..schemas import BulkMark, MarkAttendance
from ..security import CurrentAdmin
from ..services import attendance_public, employee_public, last_n_days, site_filter, upsert_attendance
from ..utils import parse_date, to_oid, today_str
from .leaves import leaves_on

router = APIRouter(prefix="/api", tags=["attendance"])


async def _day_rows(day: str, site: str | None, q: str | None = None) -> list[dict]:
    db = get_db()
    query: dict = {"is_active": True} | site_filter(site)
    if q:
        rx = {"$regex": re.escape(q.strip()), "$options": "i"}
        query["$or"] = [{"name": rx}, {"emp_code": rx}, {"pp_number": rx}, {"trade": rx}, {"site": rx}]
    emps = [e async for e in db.employees.find(query).sort("emp_code", 1)]
    recs = {
        r["employee_id"]: r
        async for r in db.attendance.find({"date": day, "employee_id": {"$in": [e["_id"] for e in emps]}})
    }
    on_leave = await leaves_on(day, [e["_id"] for e in emps])
    return [
        employee_public(e) | {"attendance": attendance_public(recs.get(e["_id"])), "on_leave": on_leave.get(e["_id"])}
        for e in emps
    ]


def _off_site(att: dict | None) -> bool:
    """True when the check-in or check-out photo was taken outside the site radius."""
    if not att:
        return False
    return any((att.get(k) or {}).get("within_site") is False for k in ("location_in", "location_out"))


def _kpis(rows: list[dict]) -> dict:
    total = len(rows)
    st = [r["attendance"]["status"] if r["attendance"] else None for r in rows]
    present, absent, half = st.count("P"), st.count("A"), st.count("HD")
    late = sum(1 for r in rows if r["attendance"] and r["attendance"]["late"])
    pf = sum(1 for r in rows if r["pf_status"] == "Verified")
    off_site = sum(1 for r in rows if _off_site(r["attendance"]))
    on_leave = sum(1 for r in rows if r.get("on_leave"))
    rate = round((present + half * 0.5) / total * 100) if total else 0
    return {
        "total": total, "present": present, "absent": absent, "half_day": half,
        "unmarked": st.count(None), "late": late, "pf_verified": pf, "attendance_rate": rate,
        "off_site": off_site, "on_leave": on_leave,
    }


@router.get("/attendance/daily")
async def daily_roster(_: CurrentAdmin, date: str | None = None, site: str | None = None, q: str | None = None):
    day = parse_date(date).isoformat() if date else today_str()
    rows = await _day_rows(day, site, q)
    all_rows = rows if not q else await _day_rows(day, site)
    return {"date": day, "kpis": _kpis(all_rows), "rows": rows}


@router.post("/attendance/mark")
async def mark(body: MarkAttendance, admin: CurrentAdmin):
    emp = await get_db().employees.find_one({"_id": to_oid(body.employee_id), "is_active": True})
    if not emp:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Employee not found")
    day = parse_date(body.date).isoformat() if body.date else today_str()
    if day > today_str():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cannot mark attendance for a future date")
    rec = await upsert_attendance(
        emp, day, body.status, check_in=body.check_in, check_out=body.check_out, source="admin", by=admin["name"]
    )
    return attendance_public(rec)


@router.post("/attendance/bulk")
async def bulk_mark(body: BulkMark, admin: CurrentAdmin):
    db = get_db()
    day = parse_date(body.date).isoformat() if body.date else today_str()
    emps = [e async for e in db.employees.find({"is_active": True} | site_filter(body.site))]
    marked = {r["employee_id"] async for r in db.attendance.find({"date": day}, {"employee_id": 1})}
    count = 0
    for e in emps:
        if body.only_unmarked and e["_id"] in marked:
            continue
        await upsert_attendance(e, day, body.status, source="admin", by=admin["name"])
        count += 1
    return {"updated": count}


@router.get("/dashboard/overview")
async def overview(_: CurrentAdmin, site: str | None = None):
    db = get_db()
    day = today_str()
    rows = await _day_rows(day, site)
    days = last_n_days(14, day)
    emp_ids = [e["_id"] async for e in db.employees.find({"is_active": True} | site_filter(site), {"_id": 1})]

    trend_map = {d: {"date": d, "P": 0, "HD": 0, "A": 0} for d in days}
    pipeline = [
        {"$match": {"date": {"$in": days}, "employee_id": {"$in": emp_ids}}},
        {"$group": {"_id": {"date": "$date", "status": "$status"}, "n": {"$sum": 1}}},
    ]
    async for r in await db.attendance.aggregate(pipeline):
        trend_map[r["_id"]["date"]][r["_id"]["status"]] = r["n"]

    sites = []
    for s in SITES if not site_filter(site) else [site]:
        srows = [r for r in rows if r["site"] == s]
        sites.append({"site": s} | _kpis(srows))

    checkins = sorted(
        (r for r in rows if r["attendance"] and r["attendance"]["check_in"]),
        key=lambda r: r["attendance"]["check_in"], reverse=True,
    )[:8]
    pending_regs = await db.regularizations.count_documents({"status": "pending"})
    pending_admins = await db.admins.count_documents({"status": "pending"})
    pending_leaves = await db.leaves.count_documents({"status": "pending"})

    return {
        "date": day,
        "kpis": _kpis(rows),
        "trend": list(trend_map.values()),
        "sites": sites,
        "recent_checkins": checkins,
        "pending_regularizations": pending_regs,
        "pending_admins": pending_admins,
        "pending_leaves": pending_leaves,
    }
