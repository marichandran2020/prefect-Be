import re

from fastapi import APIRouter, HTTPException, status
from pymongo.errors import DuplicateKeyError

from ..config import settings
from ..database import get_db
from ..schemas import EmployeeCreate, EmployeeUpdate
from ..security import CurrentAdmin
from ..services import employee_public, site_filter, upsert_attendance
from ..utils import now, to_oid, today_str

router = APIRouter(prefix="/api/employees", tags=["employees"])


def _dup_error(e: DuplicateKeyError) -> HTTPException:
    field = "PP Number" if "pp_number" in str(e) else "Employee ID"
    return HTTPException(status.HTTP_409_CONFLICT, f"{field} is already assigned to another employee")


@router.get("")
async def list_employees(
    _: CurrentAdmin,
    site: str | None = None,
    q: str | None = None,
    pf_status: str | None = None,
    include_inactive: bool = False,
):
    query: dict = site_filter(site)
    if not include_inactive:
        query["is_active"] = True
    if pf_status in ("Verified", "Pending"):
        query["pf_status"] = pf_status
    if q:
        rx = {"$regex": re.escape(q.strip()), "$options": "i"}
        query["$or"] = [{"name": rx}, {"emp_code": rx}, {"pp_number": rx}, {"trade": rx}, {"mobile": rx}]
    cursor = get_db().employees.find(query).sort("emp_code", 1)
    return [employee_public(e) async for e in cursor]


@router.post("", status_code=201)
async def create_employee(body: EmployeeCreate, admin: CurrentAdmin):
    data = body.model_dump(exclude={"initial_status"})
    data.update({"is_active": True, "photo_url": None, "created_at": now(), "created_by": admin["name"]})
    if not (data.get("uan") and data.get("esic")):
        data["pf_status"] = "Pending"
    try:
        res = await get_db().employees.insert_one(data)
    except DuplicateKeyError as e:
        raise _dup_error(e)
    emp = await get_db().employees.find_one({"_id": res.inserted_id})
    if body.initial_status:
        await upsert_attendance(emp, today_str(), body.initial_status, source="admin", by=admin["name"])
    return employee_public(emp)


@router.get("/{employee_id}")
async def get_employee(employee_id: str, _: CurrentAdmin):
    emp = await get_db().employees.find_one({"_id": to_oid(employee_id)})
    if not emp:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Employee not found")
    return employee_public(emp)


@router.patch("/{employee_id}")
async def update_employee(employee_id: str, body: EmployeeUpdate, admin: CurrentAdmin):
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Nothing to update")
    changes["updated_at"] = now()
    changes["updated_by"] = admin["name"]
    try:
        res = await get_db().employees.find_one_and_update(
            {"_id": to_oid(employee_id)}, {"$set": changes}, return_document=True
        )
    except DuplicateKeyError as e:
        raise _dup_error(e)
    if not res:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Employee not found")
    return employee_public(res)


@router.post("/{employee_id}/toggle-pf")
async def toggle_pf(employee_id: str, admin: CurrentAdmin):
    emp = await get_db().employees.find_one({"_id": to_oid(employee_id)})
    if not emp:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Employee not found")
    new = "Pending" if emp.get("pf_status") == "Verified" else "Verified"
    if new == "Verified" and not (emp.get("uan") and emp.get("esic")):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Add UAN and ESIC numbers before verifying")
    await get_db().employees.update_one(
        {"_id": emp["_id"]}, {"$set": {"pf_status": new, "pf_verified_by": admin["name"], "updated_at": now()}}
    )
    return {"pf_status": new}


def _remove_upload(url: str | None) -> bool:
    """Delete a /uploads/<file> photo from disk (only files inside UPLOAD_DIR)."""
    if not url or not url.startswith("/uploads/"):
        return False
    root = settings.UPLOAD_DIR.resolve()
    path = (root / url.removeprefix("/uploads/")).resolve()
    if path.parent != root or not path.is_file():
        return False
    path.unlink(missing_ok=True)
    return True


@router.delete("/{employee_id}")
async def delete_employee(employee_id: str, _: CurrentAdmin):
    """Permanently delete an employee and ALL of their data in every collection,
    plus their selfie / check-in photos on disk."""
    db = get_db()
    oid = to_oid(employee_id)
    emp = await db.employees.find_one({"_id": oid})
    if not emp:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Employee not found")

    # collect photo files before the records that reference them are gone
    photos = {emp.get("photo_url")}
    async for rec in db.attendance.find({"employee_id": oid}, {"selfie_in": 1, "selfie_out": 1}):
        photos.update((rec.get("selfie_in"), rec.get("selfie_out")))

    deleted = {
        "attendance": (await db.attendance.delete_many({"employee_id": oid})).deleted_count,
        "regularizations": (await db.regularizations.delete_many({"employee_id": oid})).deleted_count,
        "leaves": (await db.leaves.delete_many({"employee_id": oid})).deleted_count,
        "otps": (await db.otps.delete_many({"employee_id": oid})).deleted_count,
    }
    await db.employees.delete_one({"_id": oid})
    deleted["photos"] = sum(_remove_upload(p) for p in photos)

    return {"message": f"{emp['name']} ({emp['emp_code']}) and all related records deleted", "deleted": deleted}
