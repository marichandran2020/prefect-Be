"""Create the default super admin on first startup (no sample/demo data)."""
import logging

from .config import settings
from .database import get_db
from .security import hash_password
from .utils import now

log = logging.getLogger("seed")


async def ensure_default_admin() -> None:
    db = get_db()
    email = settings.DEFAULT_ADMIN_EMAIL.lower()
    if not await db.admins.find_one({"email": email}):
        await db.admins.insert_one({
            "name": settings.DEFAULT_ADMIN_NAME,
            "email": email,
            "site": "Both Sites (Head Office)",
            "password_hash": hash_password(settings.DEFAULT_ADMIN_PASSWORD),
            "status": "active",
            "is_super": True,
            "created_at": now(),
        })
        log.info("Default super admin created: %s", email)
