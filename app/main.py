import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import database
from .config import SITES, TRADES, settings
from .routers import admins, attendance, auth, employees, leaves, me, regularizations, reports, sites
from .seed import ensure_default_admin
from .services import refresh_sites, seed_sites

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(_: FastAPI):
    await database.connect()
    await ensure_default_admin()
    await seed_sites()
    await refresh_sites()
    yield
    await database.close()


app = FastAPI(title=settings.APP_NAME, version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=settings.UPLOAD_DIR), name="uploads")

for r in (auth, employees, attendance, regularizations, leaves, sites, me, reports, admins):
    app.include_router(r.router)


@app.get("/api/health", tags=["meta"])
async def health():
    await database.get_db().command("ping")
    return {"status": "ok"}


@app.get("/api/meta", tags=["meta"])
async def meta():
    return {"sites": SITES, "trades": TRADES, "shift_start": settings.SHIFT_START,
            "shift_end": settings.SHIFT_END, "demo_mode": settings.DEMO_MODE}
