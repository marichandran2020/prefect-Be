from fastapi import APIRouter, HTTPException, Query, status

from ..database import get_db
from ..schemas import LeaveCreate, LeaveReview
from ..security import CurrentAdmin, CurrentEmployee
from ..utils import now, parse_date, serialize, to_oid, today_str

router = APIRouter(prefix="/api", tags=["leave"])

LEAVE_TYPES = {
    "casual": "Casual Leave",
    "sick": "Sick Leave",
    "emergency": "Emergency Leave",
    "unpaid": "Unpaid Leave (LOP)",
}
MAX_BACKDATE_DAYS = 30
MAX_ADVANCE_DAYS = 90
MAX_SPAN_DAYS = 30


def leave_public(r: dict, emp: dict | None = None) -> dict:
    out = serialize(r)
    out["type_label"] = LEAVE_TYPES.get(r["type"], r["type"])
    if emp:
        out["employee"] = {
            "id": str(emp["_id"]), "name": emp["name"], "emp_code": emp["emp_code"],
            "site": emp["site"], "trade": emp["trade"], "photo_url": emp.get("photo_url"),
        }
    return out


async def leaves_on(day: str, employee_ids: list) -> dict:
    """Approved leaves covering `day`, keyed by employee id."""
    cur = get_db().leaves.find({
        "employee_id": {"$in": employee_ids}, "status": "approved",
        "from_date": {"$lte": day}, "to_date": {"$gte": day},
    })
    return {r["employee_id"]: {"type": r["type"], "type_label": LEAVE_TYPES.get(r["type"], r["type"]),
                               "half_day": r.get("half_day", False)} async for r in cur}


# ------------------------------------------------------------------ Employee side
@router.get("/me/leaves")
async def my_leaves(emp: CurrentEmployee):
    cur = get_db().leaves.find({"employee_id": emp["_id"]}).sort("created_at", -1)
    return [leave_public(r) async for r in cur]


@router.post("/me/leaves", status_code=201)
async def apply_leave(body: LeaveCreate, emp: CurrentEmployee):
    db = get_db()
    start, end = parse_date(body.from_date), parse_date(body.to_date)
    today = parse_date(today_str())
    if end < start:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "End date must be on or after the start date")
    if (today - start).days > MAX_BACKDATE_DAYS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Leave can be applied for at most {MAX_BACKDATE_DAYS} days back")
    if (start - today).days > MAX_ADVANCE_DAYS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Leave can be applied at most {MAX_ADVANCE_DAYS} days in advance")
    span = (end - start).days + 1
    if span > MAX_SPAN_DAYS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"A single request can cover at most {MAX_SPAN_DAYS} days")
    if body.half_day and span != 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Half-day leave must be for a single date")

    clash = await db.leaves.find_one({
        "employee_id": emp["_id"], "status": {"$in": ["pending", "approved"]},
        "from_date": {"$lte": end.isoformat()}, "to_date": {"$gte": start.isoformat()},
    })
    if clash:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"You already have a {clash['status']} leave from {clash['from_date']} to {clash['to_date']}",
        )

    doc = {
        "employee_id": emp["_id"],
        "site": emp["site"],
        "type": body.type,
        "from_date": start.isoformat(),
        "to_date": end.isoformat(),
        "half_day": body.half_day,
        "days": 0.5 if body.half_day else span,
        "reason": body.reason.strip(),
        "status": "pending",
        "created_at": now(),
    }
    res = await db.leaves.insert_one(doc)
    doc["_id"] = res.inserted_id
    return leave_public(doc)


@router.delete("/me/leaves/{leave_id}")
async def cancel_leave(leave_id: str, emp: CurrentEmployee):
    res = await get_db().leaves.delete_one({"_id": to_oid(leave_id), "employee_id": emp["_id"], "status": "pending"})
    if not res.deleted_count:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Only your pending leave requests can be withdrawn")
    return {"message": "Leave request withdrawn"}


# ------------------------------------------------------------------ Admin side
@router.get("/leaves")
async def list_leaves(_: CurrentAdmin, status_: str | None = Query(None, alias="status"), site: str | None = None):
    db = get_db()
    query: dict = {}
    if status_ in ("pending", "approved", "rejected"):
        query["status"] = status_
    if site and site != "ALL":
        query["site"] = site
    items = [r async for r in db.leaves.find(query).sort([("status", -1), ("from_date", -1)]).limit(500)]
    emps = {e["_id"]: e async for e in db.employees.find({"_id": {"$in": list({r["employee_id"] for r in items})}})}
    site_q = {"site": site} if site and site != "ALL" else {}
    counts = {s: await db.leaves.count_documents({"status": s} | site_q) for s in ("pending", "approved", "rejected")}
    return {"counts": counts, "items": [leave_public(r, emps.get(r["employee_id"])) for r in items]}


@router.post("/leaves/{leave_id}/review")
async def review_leave(leave_id: str, body: LeaveReview, admin: CurrentAdmin):
    db = get_db()
    leave = await db.leaves.find_one({"_id": to_oid(leave_id)})
    if not leave:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Leave request not found")
    if leave["status"] != "pending":
        raise HTTPException(status.HTTP_409_CONFLICT, f"Request already {leave['status']}")
    if body.action == "reject" and not (body.remark and body.remark.strip()):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Please add a remark explaining the rejection")
    new_status = "approved" if body.action == "approve" else "rejected"
    await db.leaves.update_one({"_id": leave["_id"]}, {"$set": {
        "status": new_status, "admin_remark": (body.remark or "").strip() or None,
        "reviewed_by": admin["name"], "reviewed_at": now(),
    }})
    return {"status": new_status}

