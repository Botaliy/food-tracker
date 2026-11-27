import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from starlette.middleware.sessions import SessionMiddleware
from starlette.staticfiles import StaticFiles

from app.config import BASE_DIR, get_settings
from app import photos, push, reminders
from app.database import SessionLocal, init_db
from app.routers import admin, api

logging.basicConfig(level=logging.INFO)
settings = get_settings()
STATIC_DIR = BASE_DIR / "app" / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    with SessionLocal() as db:
        photos.cleanup_safely(db)
    task = asyncio.create_task(reminders.loop()) if settings.scheduler_enabled and push.enabled() else None
    yield
    if task:
        task.cancel()


app = FastAPI(title="Food Tracker", lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    max_age=settings.session_days * 24 * 3600,
    same_site="lax",
    https_only=settings.secret_key != "change-me",
)
app.include_router(api.router)
app.include_router(admin.router)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# The service worker and manifest live at the root: a worker only controls pages below its own path.
@app.get("/sw.js", include_in_schema=False)
def service_worker():
    return FileResponse(STATIC_DIR / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest():
    return FileResponse(STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/.well-known/assetlinks.json", include_in_schema=False)
def asset_links():
    """Tells Android that the APK signed with this certificate may open the site full screen."""
    prints = [p.strip().upper() for p in settings.android_cert_sha256.split(",") if p.strip()]
    if not settings.android_package or not prints:
        return []
    return [{
        "relation": ["delegate_permission/common.handle_all_urls"],
        "target": {"namespace": "android_app", "package_name": settings.android_package,
                   "sha256_cert_fingerprints": prints},
    }]


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})
