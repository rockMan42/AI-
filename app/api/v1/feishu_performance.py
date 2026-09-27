import asyncio
import logging

from fastapi import HTTPException

from app.core.database import create_session
from app.schemas.permission import Role
from app.services.performance import PerformanceError, PerformanceService
from app.services.performance_cards import performance_card, performance_report_card
from app.services.role_mapper import resolve_principal

log = logging.getLogger(__name__)

async def handle_performance_card_data(data: dict) -> dict:
    event = data.get("event") or {}
    value = (event.get("action") or {}).get("value") or {}
    open_id = (event.get("operator") or {}).get("open_id", "")
    cycle = value.get("cycle")
    grade = value.get("grade")
    operation = value.get("operation")
    page = value.get("page", 1)
    if (not isinstance(cycle, str) or not 1 <= len(cycle) <= 20
            or grade not in {None, "S", "A", "B", "C", "D"}
            or operation not in {"summary", "report"}
            or type(page) is not int or not 1 <= page <= 100000):
        return {"toast": {"type": "error", "content": "绩效卡片已失效，请重新查询"}}
    try:
        async with asyncio.timeout(5):
            principal = await resolve_principal(open_id)
            async with create_session() as db:
                service = PerformanceService()
                if principal.role == Role.EMPLOYEE:
                    raise HTTPException(403, "仅主管或 HR 可查看统计报告")
                if principal.role == Role.MANAGER:
                    result = await service.team(db, principal, cycle, grade)
                    title = "团队绩效报告"
                else:
                    result = await service.overview(db, principal, cycle, grade)
                    title = "公司绩效报告"
            result_card = (performance_report_card(title, result, page)
                           if operation == "report" else performance_card(title, result))
        return {"toast": {"type": "success", "content": "绩效统计已更新"},
                "card": {"type": "raw", "data": result_card}}
    except PermissionError:
        raise HTTPException(403, "无权查看这份绩效报告") from None
    except PerformanceError as exc:
        return {"toast": {"type": "error", "content": str(exc)}}
    except HTTPException:
        raise
    except TimeoutError:
        return {"toast": {"type": "warning", "content": "统计超时，请稍后重试"}}
    except Exception:
        log.exception("performance_card_failed")
        return {"toast": {"type": "error", "content":
                "绩效统计暂时无法查看，请稍后重试"}}
