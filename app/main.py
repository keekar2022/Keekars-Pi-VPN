# Concept: Mukesh Kesharwani
# Contact: mukesh.kesharwani@adobe.com

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app import __version__, monitor, network, routing, system, wireguard
from app.auth import current_user, router as auth_router
from app.config import settings

APP_TITLE = "Keekar's Pi VPN"

logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","service.name":"pi-config-ui","message":"%(message)s"}',
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(monitor.heartbeat_loop())
    app.state.heartbeat_task = task  # strong ref so it isn't garbage-collected
    yield
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


app = FastAPI(title=APP_TITLE, version=__version__, lifespan=lifespan)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    # "strict" silently drops the cookie on the top-level cross-site
    # redirect Authentik sends back to /auth/callback, breaking every OIDC
    # login (see docs/PROJECT_NOTES.md). "lax" is the
    # standard setting for OAuth/OIDC redirect flows and still blocks the
    # cookie on cross-site POST/subresource requests.
    same_site="lax",
    https_only=True,
)

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["app_title"] = APP_TITLE
templates.env.globals["app_version"] = __version__

app.include_router(auth_router)
app.include_router(network.router)
app.include_router(routing.router)
app.include_router(monitor.router)
app.include_router(system.router)
app.include_router(wireguard.router)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data:"
    response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.get("/")
async def dashboard(request: Request, user=Depends(current_user)):
    return templates.TemplateResponse("dashboard.html", {"request": request, "user": user})


@app.get("/network")
async def network_page(request: Request, user=Depends(current_user)):
    return templates.TemplateResponse("network.html", {"request": request, "user": user})


@app.get("/routing")
async def routing_page(request: Request, user=Depends(current_user)):
    return templates.TemplateResponse("routing.html", {"request": request, "user": user})


@app.get("/wireguard")
async def wireguard_page(request: Request, user=Depends(current_user)):
    return templates.TemplateResponse("wireguard.html", {"request": request, "user": user})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logging.getLogger("pi_config_ui").exception("unhandled_exception", extra={"event": "http.unhandled_exception"})
    if request.method == "GET":
        return RedirectResponse(url="/")
    return JSONResponse(status_code=500, content={"detail": "Internal error"})
