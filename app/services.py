"""Shared attendance business logic used by admin, employee and regularization flows."""
from datetime import date as date_cls, timedelta

from bson import ObjectId

from .config import DEFAULT_SITES, SITES, settings
from .database import get_db
from .utils import STATUS_WEIGHT, distance_m, is_late, month_range, now, today_str, worked_and_ot


# ------------------------------------------------------------------ Sites
async def seed_sites() -> None:
    """First start only: create the original sites so existing employees stay valid."""
    db = get_db()
    if await db.app_meta.find_one({"_id": "sites_seeded"}):
        return
    if not await db.sites.count_documents({}):
        await db.sites.insert_many([
            {"name": n, "address": None, "latitude": None, "longitude": None,
             "radius_m": settings.DEFAULT_SITE_RADIUS_M, "created_at": now()}
            for n in DEFAULT_SITES
        ])
    await db.app_meta.update_one({"_id": "sites_seeded"}, {"$set": {"at": now()}}, upsert=True)


async def refresh_sites() -> None:
    """Reload the live SITES list (mutated in place so every importer sees the change)."""
    names = [s["name"] async for s in get_db().sites.find({}, {"name": 1}).sort("created_at", 1)]
    SITES[:] = names


async def locate(site_name: str, lat: float, lng: float, accuracy: float | None) -> dict:
    """Build the location record stored with a punch, incl. distance from the employee's site."""
    loc = {
        "latitude": lat, "longitude": lng,
        "accuracy": round(accuracy) if accuracy is not None else None,
        "site": site_name, "distance_m": None, "radius_m": None, "within_site": None,
    }
    site = await get_db().sites.find_one({"name": site_name})
    if site and site.get("latitude") is not None and site.get("longitude") is not None:
        d = distance_m(lat, lng, site["latitude"], site["longitude"])
        radius = site.get("radius_m") or settings.DEFAULT_SITE_RADIUS_M
        loc.update(distance_m=round(d), radius_m=radius, within_site=d <= radius)
    return loc


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
    location_in: dict | None = None,
    location_out: dict | None = None,
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
    if location_in:
        doc["location_in"] = location_in
    if location_out:
        doc["location_out"] = location_out
    if status == "A":
        doc.update(selfie_in=None, selfie_out=None, location_in=None, location_out=None)

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
        "location_in": rec.get("location_in"),
        "location_out": rec.get("location_out"),
        "site": rec.get("site"),
    }
