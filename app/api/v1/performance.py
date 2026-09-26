import base64
import re
from secrets import token_urlsafe
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import redis_client as redis_module
from app.core.database import get_session
from app.models.user import User
from app.schemas.performance import (
    PerformanceReportInput,
    ReminderSendInput,
)
from app.security.auth import get_current_user
from app.services.performance import PerformanceError, PerformanceService
from app.services.performance_reminder import (
    pending_reminders,
    send_reminders,
)
from app.services.performance_report import build_performance_workbook
from app.services.role_mapper import resolve_principal


router = APIRouter(prefix="/performance")
Actor = Annotated[User, Depends(get_current_user)]
DB = Annotated[AsyncSession, Depends(get_session)]
CYCLE = Query(min_length=1, max_length=20)
REPORT_TTL = 86400


def performance_http_error(exc: PerformanceError):
    raise HTTPException(exc.status_code, str(exc)) from None


@router.get("/my")
async def my_performance(actor: Actor, db: DB, cycle: str = CYCLE):
    principal = await resolve_principal(actor.feishu_open_id)
    try:
        return await PerformanceService().personal(db, principal, cycle)
    except PerformanceError as exc:
        performance_http_error(exc)


@router.get("/team")
async def team_performance(
    actor: Actor,
    db: DB,
    cycle: str = CYCLE,
    grade: str | None = Query(default=None, pattern=r"^[SABCD]$"),
):
    principal = await resolve_principal(actor.feishu_open_id)
    try:
        return await PerformanceService().team(
            db, principal, cycle, grade,
        )
    except PerformanceError as exc:
        performance_http_error(exc)


@router.get("/overview")
async def performance_overview(
    actor: Actor,
    db: DB,
    cycle: str = CYCLE,
    grade: str | None = Query(default=None, pattern=r"^[SABCD]$"),
):
    principal = await resolve_principal(actor.feishu_open_id)
    try:
        return await PerformanceService().overview(
            db, principal, cycle, grade,
        )
    except PerformanceError as exc:
        performance_http_error(exc)


@router.post("/report")
async def create_performance_report(
    body: PerformanceReportInput,
    request: Request,
    actor: Actor,
    db: DB,
):
    principal = await resolve_principal(actor.feishu_open_id)
    from app.security.permission import authorize

    await authorize(principal, "performance.report")
    try:
        rows = await PerformanceService().stats_rows(
            db,
            body.cycle,
            body.dimension,
        )
    except PerformanceError as exc:
        performance_http_error(exc)

    content = build_performance_workbook(
        body.cycle,
        body.dimension,
        rows,
    )
    client = redis_module.redis_client
    if client is None:
        raise HTTPException(503, "报告存储服务未就绪")
    token = token_urlsafe(32)
    await client.set(
        f"dep:performance:report:{token}",
        base64.b64encode(content).decode("ascii"),
        ex=REPORT_TTL,
    )
    return {
        "cycle": body.cycle,
        "dimension": body.dimension,
        "expires_in": REPORT_TTL,
        "download_url": str(request.url_for(
            "download_performance_report",
            token=token,
        )),
    }


@router.get("/reports/{token}", name="download_performance_report")
async def download_performance_report(
    token: str,
    actor: Actor,
):
    principal = await resolve_principal(actor.feishu_open_id)
    from app.security.permission import authorize

    await authorize(principal, "performance.report")
    if not re.fullmatch(r"[A-Za-z0-9_-]{40,64}", token):
        raise HTTPException(404, "报告不存在或已过期")
    client = redis_module.redis_client
    encoded = (
        await client.get(f"dep:performance:report:{token}")
        if client is not None else None
    )
    if encoded is None:
        raise HTTPException(404, "报告不存在或已过期")
    content = base64.b64decode(encoded, validate=True)
    return Response(
        content,
        media_type=(
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        ),
        headers={
            "Content-Disposition": (
                'attachment; filename="performance-report.xlsx"'
            ),
            "Cache-Control": "private, no-store",
        },
    )


@router.get("/reminders")
async def performance_reminders(actor: Actor, db: DB):
    principal = await resolve_principal(actor.feishu_open_id)
    return {
        "items": await pending_reminders(db, principal),
    }


@router.post("/reminders/send")
async def send_performance_reminders(
    body: ReminderSendInput,
    actor: Actor,
    db: DB,
):
    principal = await resolve_principal(actor.feishu_open_id)
    return await send_reminders(
        db,
        principal,
        body.review_ids,
        manual=True,
    )
