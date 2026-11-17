from fastapi import APIRouter, HTTPException, status

from ..database import get_db
from ..schemas import AdminReview
from ..security import SuperAdmin
from ..utils import now, serialize, to_oid

router = APIRouter(prefix="/api/admins", tags=["admin management"])


@router.get("")
async def list_admins(_: SuperAdmin):
    cur = get_db().admins.find({}).sort([("status", -1), ("created_at", -1)])
    return [serialize(a) async for a in cur]


@router.post("/{admin_id}/review")
async def review_admin(admin_id: str, body: AdminReview, me: SuperAdmin):
    target = await get_db().admins.find_one({"_id": to_oid(admin_id)})
    if not target:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Admin not found")
    if target["_id"] == me["_id"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You cannot change your own access")
    new = "active" if body.action == "approve" else "rejected"
    await get_db().admins.update_one(
        {"_id": target["_id"]}, {"$set": {"status": new, "reviewed_by": me["name"], "reviewed_at": now()}}
    )
    return {"status": new}
