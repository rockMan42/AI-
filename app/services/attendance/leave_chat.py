import logging
import re

from app.services.attendance.leave_cards import request_card
from app.services.attendance.leave_service import LeaveService
from app.schemas.leave import ApprovalInput
from app.services.conversation_engine.session_store import SessionStore


log = logging.getLogger(__name__)
REQUEST_ID = r"LV[0-9A-Fa-f]{32}"

"""
聊天命令
"""
async def finish_leave_flow(user_id: int, flow_key: str | None):
    if flow_key is None:
        return

    try:
        store = SessionStore()
        session = await store.load(str(user_id))
        if (
            session is not None
            and session.intent_code == "leave_apply"
            and str(session.intent_started_at) == flow_key
            and session.status == "awaiting_confirmation"
        ):
            session.status = "completed"
            session.state = "completed"
            session.workflow_state = "completed"
            session.pending_slot = None
            await store.save(session)
    except Exception as exc:
        # 数据库操作成功后，Redis异常不能把业务结果改成提交失败。
        log.error("leave_session_finish_failed error_type=%s", type(exc).__name__)


async def handle_leave_command(open_id: str, text: str):
    service = LeaveService()
    text = text.strip()

    if text in {"我的请假", "待我审批"}:
        result = await service.list_requests(
            open_id,
            scope="inbox" if text == "待我审批" else "mine",
        )
        if not result["items"]:
            return "暂无符合条件的请假申请。"

        lines = [
            f"{item['request_id']} | {item['start_date']}至"
            f"{item['end_date']} | {item['status']}"
            for item in result["items"]
        ]
        lines.append("查看详情：查询请假 申请编号")
        if result["has_more"]:
            lines.append("这里只展示最近20条，更多记录可通过列表接口分页查询。")
        return "\n".join(lines)

    match = re.fullmatch(rf"查询请假\s+({REQUEST_ID})", text)
    if match:
        data = await service.detail(open_id, match[1].upper())
        return request_card(data, can_approve=data["can_approve"])

    match = re.fullmatch(rf"撤销请假\s+({REQUEST_ID})", text)
    if match:
        await service.cancel(open_id, match[1].upper())
        return "请假申请已撤销。"

    match = re.fullmatch(rf"拒绝请假\s+({REQUEST_ID})\s+(.+)", text, re.S)
    if match:
        body = ApprovalInput(action="reject", reject_reason=match[2])
        await service.decide(open_id, match[1].upper(), body)
        return "已拒绝申请，拒绝原因将仅通知申请人。"

    # 命令格式错误不再进入模型，避免误路由成另一笔请假。
    if text.startswith(("查询请假", "撤销请假", "拒绝请假")):
        return (
            "请使用以下格式：\n"
            "查询请假 申请编号\n"
            "撤销请假 申请编号\n"
            "拒绝请假 申请编号 拒绝原因"
        )
    return None