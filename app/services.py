"""Shared attendance business logic used by admin, employee and regularization flows."""
from datetime import date as date_cls, timedelta

from bson import ObjectId

from .config import SITES, settings
from .database import get_db
from .utils import STATUS_WEIGHT, is_late, month_range, now, today_str, worked_and_ot


async def upsert_attendance(
    employee: dict,
    day: str,
    status: str,
    *,
    check_in: str | None = None,
    check_out: str | None = None,
    source: str,
    by: str,
    selfie_in: str | None = None,
    selfie_out: str | None = None,
    keep_times: bool = True,
) -> dict:
    """Create or update one employee's attendance for a day and recompute derived fields."""
    db = get_db()
    existing = await db.attendance.find_one({"employee_id": employee["_id"], "date": day}) or {}

    if status == "A":
        ci, co = None, None
    else:
        ci = check_in or (existing.get("check_in") if keep_times else None) or settings.SHIFT_START
        co = check_out or (existing.get("check_out") if keep_times else None)
        if co and co <= ci:
            co = None

    worked, ot = worked_and_ot(ci, co)
    doc = {
        "employee_id": employee["_id"],
        "site": employee["site"],
        "date": day,
        "status": status,
        "check_in": ci,
        "check_out": co,
        "worked_hours": worked,
        "ot_hours": ot,
        "late": is_late(ci),
        "source": source,
        "marked_by": by,
        "updated_at": now(),
    }
    if selfie_in:
        doc["selfie_in"] = selfie_in
    if selfie_out:
        doc["selfie_out"] = selfie_out
    if status == "A":
        doc["selfie_in"] = None
        doc["selfie_out"] = None

    await db.attendance.update_one(
        {"employee_id": employee["_id"], "date": day},
        {"$set": doc, "$setOnInsert": {"created_at": now()}},
        upsert=True,
    )
    return await db.attendance.find_one({"employee_id": employee["_id"], "date": day})


def site_filter(site: str | None) -> dict:
    return {"site": site} if site and site in SITES else {}


async def month_stats(employee_ids: list[ObjectId], month: str) -> dict[ObjectId, dict]:
    """Aggregate days worked / OT etc. per employee for a calendar month."""
    start, end = month_range(month)
    end = min(end, today_str())
    pipeline = [
        {"$match": {"employee_id": {"$in": employee_ids}, "date": {"$gte": start, "$lte": end}}},
        {"$group": {
            "_id": "$employee_id",
            "present": {"$sum": {"$cond": [{"$eq": ["$status", "P"]}, 1, 0]}},
            "half_day": {"$sum": {"$cond": [{"$eq": ["$status", "HD"]}, 1, 0]}},
            "absent": {"$sum": {"$cond": [{"$eq": ["$status", "A"]}, 1, 0]}},
            "late": {"$sum": {"$cond": ["$late", 1, 0]}},
            "ot_hours": {"$sum": "$ot_hours"},
            "worked_hours": {"$sum": "$worked_hours"},
        }},
    ]
    out: dict[ObjectId, dict] = {}
    async for row in await get_db().attendance.aggregate(pipeline):
        row["days_worked"] = row["present"] * STATUS_WEIGHT["P"] + row["half_day"] * STATUS_WEIGHT["HD"]
        out[row.pop("_id")] = row
    return out


def working_days_elapsed(month: str) -> int:
    """Calendar days of the month up to today (site works 7 days a week)."""
    start, end = month_range(month)
    end = min(end, today_str())
    if end < start:
        return 0
    return (date_cls.fromisoformat(end) - date_cls.fromisoformat(start)).days + 1


def last_n_days(n: int, until: str | None = None) -> list[str]:
    end = date_cls.fromisoformat(until or today_str())
    return [(end - timedelta(days=i)).isoformat() for i in range(n - 1, -1, -1)]


def employee_public(emp: dict) -> dict:
    return {
        "id": str(emp["_id"]),
        "emp_code": emp["emp_code"],
        "pp_number": emp["pp_number"],
        "name": emp["name"],
        "trade": emp["trade"],
        "site": emp["site"],
        "shift": emp.get("shift", "Shift A"),
        "daily_rate": emp.get("daily_rate", 0),
        "mobile": emp.get("mobile"),
        "uan": emp.get("uan"),
        "esic": emp.get("esic"),
        "pf_status": emp.get("pf_status", "Pending"),
        "photo_url": emp.get("photo_url"),
        "is_active": emp.get("is_active", True),
    }


def attendance_public(rec: dict | None) -> dict | None:
    if not rec:
        return None
    return {
        "id": str(rec["_id"]),
        "date": rec["date"],
        "status": rec["status"],
        "check_in": rec.get("check_in"),
        "check_out": rec.get("check_out"),
        "worked_hours": rec.get("worked_hours", 0),
        "ot_hours": rec.get("ot_hours", 0),
        "late": rec.get("late", False),
        "source": rec.get("source"),
        "selfie_in": rec.get("selfie_in"),
        "selfie_out": rec.get("selfie_out"),
        "site": rec.get("site"),
    }
