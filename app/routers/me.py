from fastapi import APIRouter, HTTPException, status

from ..database import get_db
from ..schemas import SelfCheck
from ..security import CurrentEmployee
from ..services import attendance_public, employee_public, month_stats, upsert_attendance, working_days_elapsed
from ..utils import month_range, now_hhmm, save_photo, today_str

router = APIRouter(prefix="/api/me", tags=["employee self-service"])


@router.get("/dashboard")
async def my_dashboard(emp: CurrentEmployee):
    db = get_db()
    day = today_str()
    month = day[:7]
    today = await db.attendance.find_one({"employee_id": emp["_id"], "date": day})
    stats = (await month_stats([emp["_id"]], month)).get(emp["_id"], {})
    recent = [attendance_public(r) async for r in db.attendance.find({"employee_id": emp["_id"]}).sort("date", -1).limit(7)]
    pending = await db.regularizations.count_documents({"employee_id": emp["_id"], "status": "pending"})
    return {
        "profile": employee_public(emp),
        "today": attendance_public(today),
        "date": day,
        "month": month,
        "stats": {
            "days_worked": stats.get("days_worked", 0),
            "present": stats.get("present", 0),
            "half_day": stats.get("half_day", 0),
            "absent": stats.get("absent", 0),
            "late": stats.get("late", 0),
            "ot_hours": stats.get("ot_hours", 0),
            "elapsed_days": working_days_elapsed(month),
            "est_earnings": round(stats.get("days_worked", 0) * emp.get("daily_rate", 0)),
        },
        "recent": recent,
        "pending_regularizations": pending,
    }


@router.post("/check-in")
async def check_in(body: SelfCheck, emp: CurrentEmployee):
    db = get_db()
    day = today_str()
    rec = await db.attendance.find_one({"employee_id": emp["_id"], "date": day})
    if rec and rec.get("check_in") and rec.get("status") != "A":
        raise HTTPException(status.HTTP_409_CONFLICT, f"Already checked in at {rec['check_in']}")
    selfie = save_photo(body.selfie, f"selfie_{emp['emp_code']}") if body.selfie else emp.get("photo_url")
    rec = await upsert_attendance(
        emp, day, "P", check_in=now_hhmm(), source="self", by=emp["name"], selfie_in=selfie, keep_times=False
    )
    return attendance_public(rec)


@router.post("/check-out")
async def check_out(body: SelfCheck, emp: CurrentEmployee):
    db = get_db()
    day = today_str()
    rec = await db.attendance.find_one({"employee_id": emp["_id"], "date": day})
    if not rec or not rec.get("check_in") or rec.get("status") == "A":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Please check in before checking out")
    if rec.get("check_out"):
        raise HTTPException(status.HTTP_409_CONFLICT, f"Already checked out at {rec['check_out']}")
    t = now_hhmm()
    if t <= rec["check_in"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Check-out time must be after check-in")
    selfie = save_photo(body.selfie, f"selfie_out_{emp['emp_code']}") if body.selfie else None
    rec = await upsert_attendance(
        emp, day, rec["status"], check_in=rec["check_in"], check_out=t, source="self", by=emp["name"], selfie_out=selfie
    )
    return attendance_public(rec)


@router.get("/attendance")
async def my_attendance(emp: CurrentEmployee, month: str | None = None):
    month = month or today_str()[:7]
    start, end = month_range(month)
    cur = get_db().attendance.find({"employee_id": emp["_id"], "date": {"$gte": start, "$lte": end}}).sort("date", 1)
    records = [attendance_public(r) async for r in cur]
    stats = (await month_stats([emp["_id"]], month)).get(emp["_id"], {})
    return {"month": month, "start": start, "end": end, "today": today_str(), "records": records, "stats": stats}
