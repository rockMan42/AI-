import asyncio
from app.core.rag_context import phase, timed

from pydantic import ValidationError
from redis import RedisError
from app.services.attendance.leave_balance_repository import (
    leave_balance_statement,
)
from app.core.database import create_session
from app.core import redis_client as redis_module
from app.models.attendance import Attendance
from app.models.leave_balance import LeaveBalance
from app.schemas.attendance import AttendanceQuery, now_shanghai, month_bounds, STATUS_VALUES, LateStats
from app.security.attendance import authorize_attendance
from sqlalchemy import select, func, case

STATS_CACHE_TTL = 60
CACHE_TIMEOUT_SECONDS = 0.1
QUERY_TIMEOUT_SECONDS = 1.5

LEAVE_LABELS = {
    "annual": "年假",
    "compensatory": "调休",
    "sick": "病假",
    "personal": "事假",
}

class AttendanceService:
    def __init__(self, session_factory = create_session):
        self.session_factory = session_factory

    @timed("attendance_total")
    async def query(self, request: AttendanceQuery, actor_open_id: str):
        async with asyncio.timeout(QUERY_TIMEOUT_SECONDS):
            return await self._query(request, actor_open_id)


    async def _query(self, request: AttendanceQuery, actor_open_id: str):
        async with self.session_factory() as db:
            with phase("attendance_pool_acquire"):
                await db.connection()
            with phase("attendance_authorize"):
                target = await authorize_attendance(db, actor_open_id, int(request.user_id))

        current = now_shanghai()
        leave_year = request.year if request.year is not None else current.year
        tasks = {}

        from app.config.settings import get_settings
        if get_settings().business_rules_enabled and request.query_type != "leave_balance":
            from app.services.business_rules.attendance_adapter import recalculate_month
            computed = await recalculate_month(request, target.user_id, self.session_factory)
            result = {
                "user_id": str(target.user_id), "user_name": target.name,
                "query_type": request.query_type, "month": request.month,
                "query_date": request.query_date.isoformat() if request.query_date else None,
                "status_filter": request.status_filter, "leave_year": leave_year,
                "queried_at": current.isoformat(), "source": "原始考勤记录，按最新规则重算",
                "rule_version": computed["rule_version"], "calculation_mode": "latest_rule",
            }
            if request.query_type in {"attendance", "punch_record", "all"}:
                result["punch_records"] = computed["punch_records"]
            if request.query_type in {"attendance", "late_count", "all"}:
                result["late_stats"] = computed["late_stats"]
            if request.query_type == "all":
                result["leave_balances"] = await self._leave_balances(target.user_id, leave_year)
            return result

        async with asyncio.TaskGroup() as group:
            if request.query_type in {
                "attendance", "punch_record", "all",
            }:
                tasks["punch_records"] = group.create_task(
                    self._punch_records(request, target.user_id)
                )

            if request.query_type in {
                "attendance", "late_count", "all",
            }:
                tasks["late_stats"] = group.create_task(
                    self._late_stats(
                        request,
                        target.user_id,
                        current.strftime("%Y-%m"),
                    )
                )

            if request.query_type in {"leave_balance", "all"}:
                tasks["leave_balances"] = group.create_task(
                    self._leave_balances(target.user_id, leave_year)
                )

        return {
            "user_id": str(target.user_id),
            "user_name": target.name,
            "query_type": request.query_type,
            "month": request.month,
            "query_date": (
                request.query_date.isoformat()
                if request.query_date else None
            ),
            "status_filter": request.status_filter,
            "leave_year": leave_year,
            "queried_at": now_shanghai().isoformat(),
            "source": "考勤数据库",
            **{
                name: task.result()
                for name, task in tasks.items()
            },
        }

    @staticmethod
    def _month_conditions(user_id: int, month: str) -> list:
        start, end = month_bounds(month)
        return [
            Attendance.user_id == user_id,
            Attendance.date >= start,
            Attendance.date < end,
        ]

    @timed("attendance_punch")
    async def _punch_records(
            self,
            request: AttendanceQuery,
            user_id: int,
    ) -> dict:
        """
        查询用户考勤记录：
            支持按月查询，支持按天查询，支持按状态查询，支持分页
        """
        conditions = self._month_conditions(user_id, request.month)

        if request.query_date is not None:
            conditions.append(Attendance.date == request.query_date)

        if request.status_filter is not None:
            conditions.append(
                Attendance.status.in_(
                    STATUS_VALUES[request.status_filter]
                )
            )


        statement = (
            select(Attendance)
            .where(*conditions)
            .order_by(Attendance.date.desc(), Attendance.id.desc())
            .offset(request.offset)
            .limit(request.limit + 1)
        )

        async with self.session_factory() as db:
            with phase("attendance_pool_acquire"):
                await db.connection()
            rows = list((await db.scalars(statement)).all())

            items = [
                {
                    "date": row.date.isoformat(),
                    "punch_in": (
                        row.clock_in_time.strftime("%H:%M")
                        if row.clock_in_time else None
                    ),
                    "punch_out": (
                        row.clock_out_time.strftime("%H:%M")
                        if row.clock_out_time else None
                    ),
                    "status": row.status,
                    "work_hours": (
                        str(row.work_hours)
                        if row.work_hours is not None else None
                    ),
                }
                for row in rows[:request.limit]
            ]

        return {
            "items": items,
            "limit": request.limit,
            "offset": request.offset,
            "has_more": len(rows) > request.limit,
        }

    @timed("attendance_stats")
    async def _late_stats(
            self,
            request: AttendanceQuery,
            user_id: int,
            current_month: str,
    ) -> dict:
        """查询用户迟到早退统计"""
        key = f"dep:attendance:stats:v1:{user_id}:{request.month}"
        use_cache = request.month == current_month

        if use_cache:
            cached = await self._read_stats_cache(key)
            with phase("attendance_cache", hit=cached is not None):
                pass
            if cached is not None:
                return {
                    **cached.model_dump(mode="json"),
                    "cached": True,
                }

        statement = select(
            func.count(Attendance.id),
            func.coalesce(
                func.sum(
                    case(
                        (Attendance.status.in_(STATUS_VALUES["late"]), 1),
                        else_=0,
                    )
                ),
                0,
            ),
            func.coalesce(
                func.sum(
                    case(
                        (
                            Attendance.status.in_(
                                STATUS_VALUES["early_leave"]
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ),
                0,
            ),
        ).where(*self._month_conditions(user_id, request.month))

        async with self.session_factory() as db:
            with phase("attendance_pool_acquire"):
                await db.connection()
            count, late, early = (await db.execute(statement)).one()

        stats = LateStats(
            record_count=int(count),
            late_count=int(late),
            early_leave_count=int(early),
            calculated_at=now_shanghai(),
        )

        if use_cache:
            await self._write_stats_cache(key, stats)

        return {
            **stats.model_dump(mode="json"),
            "cached": False,
        }

    @timed("attendance_balance")
    async def _leave_balances(
            self,
            user_id: int,
            year: int,
    ) -> list[dict]:
        """查询用户假期余额"""
        statement = leave_balance_statement(user_id,year,tuple(LEAVE_LABELS))

        async with self.session_factory() as db:
            with phase("attendance_pool_acquire"):
                await db.connection()
            rows = (await db.scalars(statement)).all()
            by_type = {row.leave_type: row for row in rows}

            balances = []
            for code, label in LEAVE_LABELS.items():
                row = by_type.get(code)
                balances.append(
                    {
                        "leave_type": code,
                        "label": label,
                        "total_days": (
                            str(row.total_days) if row is not None else None
                        ),
                        "used_days": (
                            str(row.used_days) if row is not None else None
                        ),
                        "remaining_days": (
                            str(row.remaining_days)
                            if row is not None else None
                        ),
                    }
                )

        return balances

    @timed("attendance_cache_read")
    async def _read_stats_cache(self, key: str) -> LateStats | None:
        """从缓存中读取统计信息"""
        client = redis_module.redis_client
        if client is None:
            return None

        try:
            async with asyncio.timeout(CACHE_TIMEOUT_SECONDS):
                raw = await client.get(key)

            return LateStats.model_validate_json(raw) if raw else None
        except (RedisError, TimeoutError, ValidationError):
            return None

    @timed("attendance_cache_write")
    async def _write_stats_cache(
            self,
            key: str,
            stats: LateStats,
    ) -> None:
        """将统计信息写入缓存"""
        client = redis_module.redis_client
        if client is None:
            return

        try:
            async with asyncio.timeout(CACHE_TIMEOUT_SECONDS):
                await client.set(
                    key,
                    stats.model_dump_json(),
                    ex=STATS_CACHE_TTL,
                )
        except (RedisError, TimeoutError):
            pass
