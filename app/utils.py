import base64
import binascii
import math
import re
import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import HTTPException, status

from .config import settings

TZ = ZoneInfo(settings.TIMEZONE)
STATUS_WEIGHT = {"P": 1.0, "HD": 0.5, "A": 0.0}
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def now() -> datetime:
    return datetime.now(TZ)


def today_str() -> str:
    return now().date().isoformat()


def now_hhmm() -> str:
    return now().strftime("%H:%M")


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid date, expected YYYY-MM-DD")


def month_range(month: str) -> tuple[str, str]:
    """'2026-11' -> ('2026-11-01', '2026-11-30')"""
    try:
        start = datetime.strptime(month + "-01", "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid month, expected YYYY-MM")
    nxt = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start.isoformat(), (nxt - timedelta(days=1)).isoformat()


def to_oid(value: str) -> ObjectId:
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Record not found")


def minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def worked_and_ot(check_in: str | None, check_out: str | None) -> tuple[float, float]:
    if not check_in or not check_out:
        return 0.0, 0.0
    worked = max(0, minutes(check_out) - minutes(check_in)) / 60
    shift = (minutes(settings.SHIFT_END) - minutes(settings.SHIFT_START)) / 60
    ot = max(0.0, worked - shift)
    return round(worked, 2), round(ot * 2) / 2  # OT in 0.5 hr steps


def is_late(check_in: str | None) -> bool:
    if not check_in:
        return False
    return minutes(check_in) > minutes(settings.SHIFT_START) + settings.LATE_GRACE_MINUTES


def distance_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle (haversine) distance between two GPS points, in metres."""
    r = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def save_photo(data_url: str | None, prefix: str) -> str | None:
    """Decode a base64 data URL, store it under /uploads and return its public path."""
    if not data_url:
        return None
    match = re.match(r"^data:image/(png|jpe?g|webp);base64,(.+)$", data_url, re.DOTALL)
    if not match:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Photo must be a PNG, JPEG or WEBP image")
    ext = "jpg" if match.group(1).startswith("jp") else match.group(1)
    try:
        raw = base64.b64decode(match.group(2), validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Photo data is corrupted")
    if len(raw) > settings.MAX_PHOTO_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Photo is larger than 3 MB")
    settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{prefix}_{uuid.uuid4().hex[:12]}.{ext}"
    (settings.UPLOAD_DIR / name).write_bytes(raw)
    return f"/uploads/{name}"


def serialize(doc: dict | None) -> dict | None:
    """Convert Mongo document to JSON-friendly dict (ObjectId -> str, _id -> id)."""
    if doc is None:
        return None
    out = {}
    for k, v in doc.items():
        if k == "_id":
            out["id"] = str(v)
        elif k == "password_hash":
            continue
        elif isinstance(v, ObjectId):
            out[k] = str(v)
        elif isinstance(v, datetime):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out
