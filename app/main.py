"""FastAPI app factory and lifecycle.

Lifespan handler owns the TPC client and scheduler. Both are torn down
cleanly on shutdown so the systemd unit restarts predictably.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from app import scheduler as scheduler_mod
from app.config import get_settings
from app.db import init_schema
from app.effects import EffectEngine
from app.routes import admin, auth, controls, dashboard, effects, overrides
from app.tpc import TPCClient

logger = logging.getLogger(__name__)


def _text_on(hex_color: str | None) -> str:
    """Pick black or white text for readable contrast on a hex background."""
    h = (hex_color or "").lstrip("#")
    if len(h) != 6:
        return "#ffffff"
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return "#ffffff"
    lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    return "#000000" if lum > 0.6 else "#ffffff"


# Shared TPC client; routes get it via request.app.state.tpc
def _build_tpc_client() -> TPCClient:
    s = get_settings()
    return TPCClient(
        base_url=s.tpc_base_url,
        username=s.tpc_username,
        password=s.tpc_password,
        verify_ssl=s.tpc_verify_ssl,
        timeout_seconds=s.tpc_request_timeout_seconds,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger.info("Initialising DB schema")
    init_schema()

    logger.info("Building TPC client")
    tpc = _build_tpc_client()
    app.state.tpc = tpc

    logger.info("Starting effect engine")
    app.state.effect_engine = EffectEngine(tpc)

    logger.info("Starting scheduler")
    scheduler_mod.init(tpc, app.state.effect_engine)

    try:
        yield
    finally:
        logger.info("Stopping effect engine")
        app.state.effect_engine.stop()
        logger.info("Shutting down scheduler")
        scheduler_mod.shutdown()
        logger.info("Closing TPC client")
        tpc.close()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Pharos Overrides Scheduler", lifespan=lifespan)

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.app_secret_key,
        same_site="lax",
        https_only=False,  # behind Caddy with HTTPS this would be True; dev=false
    )

    # Templates + static
    base = Path(__file__).resolve().parent
    templates = Jinja2Templates(directory=str(base / "templates"))
    templates.env.filters["text_on"] = _text_on
    app.state.templates = templates

    static_dir = base / "static"
    static_dir.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Routes
    app.include_router(auth.router)
    app.include_router(dashboard.router)
    app.include_router(controls.router)
    app.include_router(effects.router)
    app.include_router(overrides.router)
    app.include_router(admin.router)

    @app.get("/", include_in_schema=False)
    def _root(request: Request) -> RedirectResponse:
        return RedirectResponse(url="/dashboard")

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(request: Request, exc: StarletteHTTPException) -> Response:
        # Browsers (and HTMX requests) should be sent to the login page on 401
        # rather than getting raw JSON.
        if exc.status_code == 401:
            accept = request.headers.get("accept", "")
            is_htmx = request.headers.get("hx-request") == "true"
            if is_htmx:
                # HTMX-aware redirect: tells the client to navigate to /login
                return Response(status_code=204, headers={"HX-Redirect": "/login"})
            if "text/html" in accept or "*/*" in accept:
                return RedirectResponse(url="/login", status_code=303)
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    return app


app = create_app()
