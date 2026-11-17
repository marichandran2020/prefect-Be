from fastapi import APIRouter, HTTPException, status
from pymongo.errors import DuplicateKeyError

from ..config import settings
from ..database import get_db
from ..schemas import SiteCreate, SiteUpdate
from ..security import CurrentAdmin
from ..services import refresh_sites
from ..utils import now, to_oid

router = APIRouter(prefix="/api/sites", tags=["sites"])

# Collections that store the site *name* on each document.
_SITE_REFS = ("employees", "attendance", "regularizations", "leaves", "admins")


def site_public(s: dict, employees: int = 0) -> dict:
    return {
        "id": str(s["_id"]),
        "name": s["name"],
        "address": s.get("address"),
        "latitude": s.get("latitude"),
        "longitude": s.get("longitude"),
        "radius_m": s.get("radius_m") or settings.DEFAULT_SITE_RADIUS_M,
        "has_location": s.get("latitude") is not None and s.get("longitude") is not None,
        "employees": employees,
    }


def _dup() -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, "A site with this name already exists")


def _check_coords(lat: float | None, lng: float | None) -> None:
    if (lat is None) != (lng is None):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Enter both latitude and longitude, or leave both empty")


@router.get("")
async def list_sites(_: CurrentAdmin):
    db = get_db()
    counts = {
        r["_id"]: r["n"]
        async for r in await db.employees.aggregate([
            {"$match": {"is_active": True}}, {"$group": {"_id": "$site", "n": {"$sum": 1}}},
        ])
    }
    return [site_public(s, counts.get(s["name"], 0)) async for s in db.sites.find({}).sort("created_at", 1)]


@router.post("", status_code=201)
async def add_site(body: SiteCreate, admin: CurrentAdmin):
    _check_coords(body.latitude, body.longitude)
    data = body.model_dump() | {"created_at": now(), "created_by": admin["name"]}
    try:
        res = await get_db().sites.insert_one(data)
    except DuplicateKeyError:
        raise _dup()
    await refresh_sites()
    return site_public(data | {"_id": res.inserted_id})


@router.patch("/{site_id}")
async def update_site(site_id: str, body: SiteUpdate, admin: CurrentAdmin):
    db = get_db()
    site = await db.sites.find_one({"_id": to_oid(site_id)})
    if not site:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Site not found")
    changes = body.model_dump(exclude_unset=True)
    if "name" in changes and not changes["name"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Site name cannot be empty")
    lat = changes.get("latitude", site.get("latitude"))
    lng = changes.get("longitude", site.get("longitude"))
    _check_coords(lat, lng)
    if not changes:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Nothing to update")
    changes |= {"updated_at": now(), "updated_by": admin["name"]}
    try:
        updated = await db.sites.find_one_and_update({"_id": site["_id"]}, {"$set": changes}, return_document=True)
    except DuplicateKeyError:
        raise _dup()

    # Renamed: carry the new name over to every record that references the old one.
    old, new = site["name"], updated["name"]
    if new != old:
        for coll in _SITE_REFS:
            await db[coll].update_many({"site": old}, {"$set": {"site": new}})
        await db.employees.update_many({"last_site": old}, {"$set": {"last_site": new}})
    await refresh_sites()
    return site_public(updated, await db.employees.count_documents({"site": new, "is_active": True}))


@router.delete("/{site_id}")
async def delete_site(site_id: str, _: CurrentAdmin):
    db = get_db()
    site = await db.sites.find_one({"_id": to_oid(site_id)})
    if not site:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Site not found")
    assigned = await db.employees.count_documents({"site": site["name"]})
    if assigned:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{assigned} employee(s) are assigned to {site['name']}. Move them to another site first.",
        )
    await db.sites.delete_one({"_id": site["_id"]})
    await refresh_sites()
    return {"message": f"Site {site['name']} deleted"}
