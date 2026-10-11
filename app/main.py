"""FastAPI application wiring."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import catalog, chat, deploy, health, publish
from app.config import ROOT, get_settings
from app.context.store import get_store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("contextengine")


@asynccontextmanager
async def lifespan(_: FastAPI):
    get_store().initialize()
    settings = get_settings()
    if not settings.framework_repo.exists():
        log.warning("framework_repo %s does not exist - codegen will be unable to load the config schema", settings.framework_repo)
    yield


app = FastAPI(title="ContextEngine", version="0.1.0", lifespan=lifespan)

# The Vite dev server runs on its own origin during frontend development.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(catalog.router)
app.include_router(chat.router)
app.include_router(publish.router)
app.include_router(deploy.router)


@app.exception_handler(Exception)
async def unhandled(request, exc: Exception):
    log.exception("unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"error": type(exc).__name__, "detail": str(exc)})


_DIST = ROOT / "web" / "dist"
if _DIST.exists():
    app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(_DIST / "index.html")

else:

    @app.get("/")
    def index_missing() -> JSONResponse:
        return JSONResponse(
            {
                "message": "Frontend not built. Run `npm install && npm run build` in web/, "
                "or `npm run dev` for the Vite dev server on port 5173.",
                "api_docs": "/docs",
            }
        )
