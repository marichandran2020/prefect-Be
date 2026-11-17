from pymongo import ASCENDING, DESCENDING, AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import ServerSelectionTimeoutError

from .config import settings

client: AsyncMongoClient | None = None
db: AsyncDatabase | None = None


async def connect() -> None:
    global client, db
    client = AsyncMongoClient(settings.MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
    db = client[settings.MONGO_DB]
    try:
        await client.admin.command("ping")
    except ServerSelectionTimeoutError:
        host = settings.MONGO_URI.split("@")[-1].split("/")[0]  # hide credentials
        raise RuntimeError(
            f"\n\n  Cannot connect to MongoDB at '{host}'.\n"
            "  -> Start a local MongoDB server, or set MONGO_URI in perfect-Be/.env\n"
            "     to your MongoDB Atlas connection string (mongodb+srv://...).\n"
        ) from None
    await ensure_indexes()


async def close() -> None:
    if client is not None:
        await client.close()


def get_db() -> AsyncDatabase:
    if db is None:
        raise RuntimeError("Database not initialised")
    return db


async def ensure_indexes() -> None:
    d = get_db()
    await d.admins.create_index("email", unique=True)
    await d.employees.create_index("emp_code", unique=True)
    await d.employees.create_index("pp_number", unique=True)
    await d.employees.create_index([("site", ASCENDING), ("is_active", ASCENDING)])
    await d.attendance.create_index([("employee_id", ASCENDING), ("date", ASCENDING)], unique=True)
    await d.attendance.create_index("date")
    await d.regularizations.create_index([("status", ASCENDING), ("created_at", DESCENDING)])
    await d.regularizations.create_index("employee_id")
    await d.sites.create_index("name", unique=True)
    await d.leaves.create_index([("status", ASCENDING), ("created_at", DESCENDING)])
    await d.leaves.create_index([("employee_id", ASCENDING), ("from_date", ASCENDING)])
    await d.otps.create_index("expires_at", expireAfterSeconds=0)
    await d.password_resets.create_index("expires_at", expireAfterSeconds=0)
