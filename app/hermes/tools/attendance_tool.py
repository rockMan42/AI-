import asyncio
import json

from concurrent.futures import TimeoutError as FutureTimeoutError
from pydantic import ValidationError
from tools.registry import registry
from app.schemas.attendance import AttendanceQuery
from app.services.attendance.attendance_service import AttendanceService
from app.services.attendance.feishu_card import build_attendance_card

TOOL_NAME = "query_attendance"
TOOLSET = "enterprise_attendance"



def error_result(status_code: int, message: str) -> dict:
    return {
        "success": False,
        "status_code": status_code,
        "message": message,
    }


async def execute_query(args: dict, actor_open_id: str) -> dict:
    try:
        """执行考勤查询"""
        request = AttendanceQuery.model_validate(args)
        request = request.model_copy(update={"limit":5,"offset":0})
        result = await AttendanceService().query(request,actor_open_id)
    except ValidationError:
        return error_result(422, "查询参数无效，请检查月份和用户 ID")
    except PermissionError:
        return error_result(403, "无权查询该员工的考勤")
    except TimeoutError:
        return error_result(504, "考勤查询超时，请稍后重试")
    except Exception:
        # 不返回 SQL、参数、异常正文或考勤明细。
        return error_result(503, "考勤服务暂时不可用")

    return {
        "success": True,
        "status_code": 200,
        "message": "考勤查询完成",
        "data": result,
        "card": build_attendance_card(result),
    }

def register_attendance_tool(loop: asyncio.AbstractEventLoop) -> None:
    def handler(
            args: dict,
            *,
            actor_open_id: str | None = None,
            **kwargs
    ) -> str:
        if not actor_open_id:
            payload = error_result(status_code=403, message="缺少可信的操作者")
        else:
            future = asyncio.run_coroutine_threadsafe(
                execute_query(args,actor_open_id),
                loop
            )

            try:
                payload = future.result(timeout=2.0)
            except FutureTimeoutError:
                future.cancel()
                payload = error_result(504, "考勤查询超时")
            except Exception:
                payload = error_result(503, "考勤服务暂时不可用")

        return json.dumps(payload)

    parameters = AttendanceQuery.model_json_schema()
    parameters["required"] = ["user_id", "query_type"]

    # HTTP 分页参数不暴露给模型。
    for name in ("limit", "offset"):
        parameters["properties"].pop(name, None)

    registry.register(
        name=TOOL_NAME,
        toolset=TOOLSET,
        schema={
            "name": TOOL_NAME,
            "description": (
               "查询员工考勤打卡、月度迟到早退统计和指定年度假期余额，年度未指定时默认今年。"
                "员工仅可查询自己，HR 可查询同部门成员。"
            ),
            "parameters": parameters,
        },
        handler=handler,
        override=True,
    )
