from fastapi import APIRouter, HTTPException, Query, status

from ..database import get_db
from ..schemas import RegularizationCreate, RegularizationReview
from ..security import CurrentAdmin, CurrentEmployee
from ..services import upsert_attendance
from ..utils import now, parse_date, serialize, to_oid, today_str

router = APIRouter(prefix="/api", tags=["regularization"])

REG_TYPES = {
    "missed_punch": "Missed Punch",
    "wrong_status": "Wrong Status Marked",
    "late_arrival": "Late Arrival",
    "early_exit": "Early Exit",
    "on_duty": "On Duty / Outdoor Work",
}
MAX_BACKDATE_DAYS = 30


def reg_public(r: dict, emp: dict | None = None) -> dict:
    out = serialize(r)
    out["type_label"] = REG_TYPES.get(r["type"], r["type"])
    if emp:
        out["employee"] = {
            "id": str(emp["_id"]), "name": emp["name"], "emp_code": emp["emp_code"],
            "site": emp["site"], "trade": emp["trade"], "photo_url": emp.get("photo_url"),
        }
    return out


# ------------------------------------------------------------------ Employee side
@router.get("/me/regularizations")
async def my_regularizations(emp: CurrentEmployee):
    cur = get_db().regularizations.find({"employee_id": emp["_id"]}).sort("created_at", -1)
    return [reg_public(r) async for r in cur]


@router.post("/me/regularizations", status_code=201)
async def apply_regularization(body: RegularizationCreate, emp: CurrentEmployee):
    db = get_db()
    day = parse_date(body.date)
    today = parse_date(today_str())
    if day > today:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You cannot regularize a future date")
    if (today - day).days > MAX_BACKDATE_DAYS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Requests are allowed only for the last {MAX_BACKDATE_DAYS} days")
    if body.requested_status != "A" and not body.check_in:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Check-in time is required for Present / Half-Day")
    if body.check_in and body.check_out and body.check_out <= body.check_in:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Check-out must be after check-in")
    if await db.regularizations.find_one({"employee_id": emp["_id"], "date": day.isoformat(), "status": "pending"}):
        raise HTTPException(status.HTTP_409_CONFLICT, "A pending request already exists for this date")

    current = await db.attendance.find_one({"employee_id": emp["_id"], "date": day.isoformat()})
    doc = {
        "employee_id": emp["_id"],
        "site": emp["site"],
        "date": day.isoformat(),
        "type": body.type,
        "requested_status": body.requested_status,
        "check_in": body.check_in,
        "check_out": body.check_out,
        "reason": body.reason.strip(),
        "original": {
            "status": current.get("status") if current else None,
            "check_in": current.get("check_in") if current else None,
            "check_out": current.get("check_out") if current else None,
        },
        "status": "pending",
        "created_at": now(),
    }
    res = await db.regularizations.insert_one(doc)
    doc["_id"] = res.inserted_id
    return reg_public(doc)


@router.delete("/me/regularizations/{reg_id}")
async def cancel_regularization(reg_id: str, emp: CurrentEmployee):
    res = await get_db().regularizations.delete_one(
        {"_id": to_oid(reg_id), "employee_id": emp["_id"], "status": "pending"}
    )
    if not res.deleted_count:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Only your pending requests can be withdrawn")
    return {"message": "Request withdrawn"}


# ------------------------------------------------------------------ Admin side
@router.get("/regularizations")
async def list_regularizations(_: CurrentAdmin, status_: str | None = Query(None, alias="status"), site: str | None = None):
    db = get_db()
    query: dict = {}
    if status_ in ("pending", "approved", "rejected"):
        query["status"] = status_
    if site:
        query["site"] = site
    regs = [r async for r in db.regularizations.find(query).sort([("status", -1), ("created_at", -1)]).limit(500)]
    emps = {e["_id"]: e async for e in db.employees.find({"_id": {"$in": list({r["employee_id"] for r in regs})}})}
    counts = {s: await db.regularizations.count_documents({"status": s} | ({"site": site} if site else {}))
              for s in ("pending", "approved", "rejected")}
    return {"counts": counts, "items": [reg_public(r, emps.get(r["employee_id"])) for r in regs]}


@router.post("/regularizations/{reg_id}/review")
async def review_regularization(reg_id: str, body: RegularizationReview, admin: CurrentAdmin):
    db = get_db()
    reg = await db.regularizations.find_one({"_id": to_oid(reg_id)})
    if not reg:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Request not found")
    if reg["status"] != "pending":
        raise HTTPException(status.HTTP_409_CONFLICT, f"Request already {reg['status']}")
    if body.action == "reject" and not (body.remark and body.remark.strip()):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Please add a remark explaining the rejection")

    if body.action == "approve":
        emp = await db.employees.find_one({"_id": reg["employee_id"]})
        if not emp:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Employee no longer exists")
        await upsert_attendance(
            emp, reg["date"], reg["requested_status"], check_in=reg.get("check_in"), check_out=reg.get("check_out"),
            source="regularization", by=admin["name"], keep_times=False,
        )

    new_status = "approved" if body.action == "approve" else "rejected"
    await db.regularizations.update_one({"_id": reg["_id"]}, {"$set": {
        "status": new_status, "admin_remark": (body.remark or "").strip() or None,
        "reviewed_by": admin["name"], "reviewed_at": now(),
    }})
    return {"status": new_status}
