import asyncio
import logging

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from app.core.database import create_session
from app.models import User
from app.schemas.attendance import AttendanceQuery
from app.security.attendance import authorize_attendance
from app.security.auth import ACTIVE_STATUSES
from app.services.attendance.attendance_service import AttendanceService
from app.services.attendance.feishu_card import (
    attendance_filter_card, build_attendance_card_v2,
)
from app.services.card_form import parse_form_values

log = logging.getLogger(__name__)

async def handle_attendance_card_data(data: dict) -> dict:
    event = data.get("event") or {}
    action = event.get("action") or {}
    value = action.get("value") or {}
    open_id = (event.get("operator") or {}).get("open_id", "")
    target_id = value.get("target_user_id")
    if isinstance(target_id, str) and target_id.isdigit():
        target_id = int(target_id)
    if type(target_id) is not int or target_id <= 0:
        return {"toast": {"type": "error", "content": "考勤卡片已失效，请重新查询"}}

    try:
        async with asyncio.timeout(5):
            async with create_session() as db:
                actor = await db.scalar(select(User).where(User.feishu_open_id == open_id))
                if actor is None or actor.status not in ACTIVE_STATUSES:
                    raise HTTPException(403, "用户不存在或已停用")

                fields = action.get("form_value") or {}
                operation = value.get("operation")
                if operation == "query":
                    allowed = {"query_type", "month", "query_date", "status_filter",
                               "target_name"}
                    fields = parse_form_values(fields, allowed)
                    target_name = fields.get("target_name", "").strip()
                    if target_name:
                        candidates = (await db.scalars(select(User).where(
                            User.name == target_name,
                            User.status.in_(ACTIVE_STATUSES),
                        ).limit(2))).all()
                        if len(candidates) != 1:
                            raise ValueError("未找到唯一的在职员工，请输入完整姓名")
                        target_id = candidates[0].user_id

                target = await authorize_attendance(db, open_id, target_id)

            if operation == "filter":
                from app.schemas.attendance import now_shanghai
                result_card = attendance_filter_card(
                    target_id, target.name, now_shanghai().strftime("%Y-%m"),
                    value.get("query_type") or "attendance",
                )
                message = "请选择查询范围"
            elif operation == "query":
                query_date = fields.get("query_date", "").strip() or None
                month = fields.get("month", "").strip() or None
                if query_date:
                    month = query_date[:7]
                query_type = fields.get("query_type") or "attendance"
                request = AttendanceQuery(
                    user_id=str(target_id),
                    query_type=query_type,
                    month=month, query_date=query_date,
                    status_filter=(fields.get("status_filter") or None)
                    if query_type != "leave_balance" else None,
                )
                if query_type == "leave_balance":
                    request.year = int(request.month[:4])
                result_card = build_attendance_card_v2(
                    await AttendanceService().query(request, open_id)
                )
                message = "考勤查询完成"
            else:
                raise ValueError("考勤卡片已失效，请重新查询")
        return {"toast": {"type": "success", "content": message},
                "card": {"type": "raw", "data": result_card}}
    except PermissionError:
        raise HTTPException(403, "无权查询该员工的考勤") from None
    except HTTPException:
        raise
    except ValidationError:
        return {"toast": {"type": "error", "content":
                "请检查月份、日期和筛选条件"}}
    except ValueError as exc:
        return {"toast": {"type": "error", "content": str(exc)}}
    except TimeoutError:
        return {"toast": {"type": "warning", "content": "查询超时，请稍后重试"}}
    except Exception:
        log.exception("attendance_card_failed")
        return {"toast": {"type": "error", "content":
                "考勤查询暂时无法完成，请稍后重试"}}
