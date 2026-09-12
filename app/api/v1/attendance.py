from typing import Annotated

from fastapi import APIRouter
from fastapi import Depends, Query, HTTPException
from pydantic import ValidationError

from app.models import User
from app.schemas.attendance import AttendanceQuery
from app.security.auth import get_current_user
from app.core.database import get_session
from app.core.rag_context import traced, timed
from sqlalchemy.ext.asyncio import AsyncSession
from app.services.attendance.attendance_service import AttendanceService


async def attendance_actor(
    actor: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> User:
    # 该路由只有认证读取；脱离会话后关闭事务，不影响其他业务依赖。
    db.expunge(actor)
    await db.close()
    return actor


router = APIRouter(prefix="/attendance")

async def attendance_parameters(
        actor: Annotated[User, Depends(attendance_actor)],
        user_id: str | None = None,
        month: str | None = None,
        query_date: str | None = None,
        status_filter: str | None = None,
        year: int | None = Query(default=None, ge=1900, le=9999),
        limit: int = Query(default=5, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
) -> tuple[User, AttendanceQuery]:
    """构造请求参数"""
    try:
        request = AttendanceQuery(
            user_id=user_id if user_id is not None else str(actor.user_id),
            month=month,
            year=year,
            query_date=query_date,
            status_filter=status_filter,
            limit=limit,
            offset=offset,
        )
    except ValidationError:
        raise HTTPException(
            status_code=422,
            detail="考勤查询参数无效",
        ) from None

    return actor, request


AttendanceParameters = Annotated[tuple[User, AttendanceQuery], Depends(attendance_parameters)]

async def run_query(
    parameters: tuple[User, AttendanceQuery],
    query_type: str,
) -> dict:
    actor, request = parameters
    request = request.model_copy(update={"query_type":query_type})

    try:
        return await AttendanceService().query(request,actor.feishu_open_id)
    except PermissionError:
        raise HTTPException(
            status_code=403,
            detail="无权查询该员工的考勤",
        ) from None
    except TimeoutError:
        raise HTTPException(
            status_code=504,
            detail="考勤查询超时",
        ) from None
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="考勤服务暂时不可用",
        ) from None


@router.get("/punch-records")
@traced
@timed("attendance_api")
async def punch_records(parameters: AttendanceParameters):
    return await run_query(parameters, "punch_record")


@router.get("/late-stats")
@traced
@timed("attendance_api")
async def late_stats(parameters: AttendanceParameters):
    return await run_query(parameters, "late_count")


@router.get("/leave-balance")
@traced
@timed("attendance_api")
async def leave_balance(parameters: AttendanceParameters):
    return await run_query(parameters, "leave_balance")