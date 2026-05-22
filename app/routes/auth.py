"""Auth routes — inert.

The app runs in always-logged-in mode via APP_AUTO_LOGIN_AS. The login form and
logout endpoint are kept as redirects so any bookmarked URL lands on the
dashboard instead of 404ing.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import RedirectResponse

router = APIRouter()


@router.get("/login")
@router.post("/login")
@router.post("/logout")
def _redirect_home() -> RedirectResponse:
    return RedirectResponse(url="/dashboard", status_code=303)
