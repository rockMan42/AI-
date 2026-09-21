from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.config.settings import get_settings
from app.core.database import create_session
from app.models.permission import AuthSession
from app.models.user import User
from app.schemas.permission import RefreshInput
from app.security.auth import get_current_user
from app.security.permission import permissions
from app.services.auth import begin_login, complete_login, decode_access, refresh_login
from app.services.role_mapper import resolve_principal


router = APIRouter(prefix="/auth")
COOKIE_NAME = "permission_oauth_state"


@router.get("/login")
async def login():
    url, state = await begin_login()
    settings = get_settings()

    response = RedirectResponse(url)
    response.set_cookie(
        COOKIE_NAME,
        state,
        max_age=300,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        path=f"{settings.app_prefix}/auth/callback",
    )
    return response


@router.get("/callback")
async def callback(
    request: Request,
    code: str = Query(min_length=1, max_length=2048),
    state: str = Query(min_length=1, max_length=200),
):
    payload = await complete_login(
        code,
        state,
        request.cookies.get(COOKIE_NAME, ""),
    )

    response = JSONResponse(payload, headers={"Cache-Control": "no-store"})
    response.delete_cookie(
        COOKIE_NAME,
        path=f"{get_settings().app_prefix}/auth/callback",
    )
    return response


@router.post("/refresh")
async def refresh(body: RefreshInput):
    return JSONResponse(
        await refresh_login(body.refresh_token),
        headers={"Cache-Control": "no-store"},
    )


@router.post("/logout")
async def logout(request: Request, user: User = Depends(get_current_user)):
    token = request.headers["Authorization"].split(" ", 1)[1]
    claims = decode_access(token)

    async with create_session() as db, db.begin():
        session = await db.get(AuthSession, claims["sid"], with_for_update=True)
        if session:
            session.revoked = True

    return {"message": "已退出登录"}


@router.get("/role")
async def current_role(user: User = Depends(get_current_user)):
    principal = await resolve_principal(user.feishu_open_id)
    return principal.model_dump(mode="json")


@router.get("/permissions")
async def current_permissions(user: User = Depends(get_current_user)):
    principal = await resolve_principal(user.feishu_open_id)
    return permissions(principal)