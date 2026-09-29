# Concept: Mukesh Kesharwani
# Contact: mukesh.kesharwani@adobe.com

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app import __version__

APP_TITLE = "Keekar's Pi VPN"

templates = Jinja2Templates(directory="app/templates")
templates.env.globals["app_title"] = APP_TITLE
templates.env.globals["app_version"] = __version__


def error_page(request: Request, status_code: int, title: str, message: str,
               link_href: str, link_text: str, headers: dict | None = None):
    # A page, never a redirect: redirecting on failure is what looped / <-> /auth/login.
    return templates.TemplateResponse(
        request,
        "error.html",
        {"title": title, "message": message, "link_href": link_href, "link_text": link_text},
        status_code=status_code,
        headers=headers,
    )
