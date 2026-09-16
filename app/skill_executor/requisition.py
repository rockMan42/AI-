from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User
from app.schemas.skill_context import SkillContext
from app.services.requisition.cards import requisition_confirmation_card
from app.services.requisition.requisition_service import RequisitionService, RequisitionError
from app.skill_executor.base import BaseSkillExecutor, SkillResult
from app.hermes.requisition_mcp_client import (
    call_requisition_tool,
)

class RequisitionSkillExecutor(BaseSkillExecutor):

    async def executor(self,context: SkillContext, slots: dict, db: AsyncSession) -> SkillResult:
        service = RequisitionService()
        user_id = context.user_id
        user = await db.get(User, user_id)
        if user is None:
            return SkillResult(
                success=False,
                message="用户不存在"
            )

        try:
            draft = await service.prepare(db, user, slots)
        except RequisitionError as exc:
            return SkillResult(False, str(exc))
        except ValueError:
            return SkillResult(False, "申领信息格式不正确，请检查后重试")

        return SkillResult(
            success=True,
            message="请确认申领信息，确认后才会提交",
            data={"awaiting_confirmation": True},
            card=requisition_confirmation_card(draft),
        )


class RequisitionStatusQueryExecutor(BaseSkillExecutor):
    async def executor(
        self,
        context: SkillContext,
        slots: dict,
        db: AsyncSession,
    ) -> SkillResult:
        service = RequisitionService()

        requisition_id = slots.get("requisition_id")
        if requisition_id is None:
            row = await service.latest_mine(
                db,
                int(context.user_id),
            )
        else:
            from app.models.requisition import Requisition

            row = await db.get(
                Requisition,
                int(requisition_id),
            )
            if (
                row is not None
                and row.user_id != int(context.user_id)
            ):
                return SkillResult(
                    False,
                    "无权查看其他员工的申领单",
                )

        if row is None:
            return SkillResult(
                False,
                "没有找到您的物资申领记录",
            )

        try:
            data = await call_requisition_tool(
                "query_requisition",
                {"requisition_id": row.id},
            )
        except Exception:
            return SkillResult(
                False,
                "OA 状态暂时无法查询，请稍后重试",
            )

        lines = [
            f"申领单号：REQ-{row.id}",
            f"物资：{row.item_name} × {row.quantity}",
            f"当前状态：{data.get('status_text', data['status'])}",
        ]

        current = data.get("current_approver")
        if isinstance(current, dict):
            current = current.get("approver_name")
        if current:
            lines.append(f"当前审批人：{current}")

        chain = data.get("approval_chain") or []
        if chain:
            chain_text = " → ".join(
                f"{node.get('approver_name', '审批人')}"
                f"{'✅' if node.get('action') in {'approve', 'approved'} else '🔄'}"
                for node in chain
            )
            lines.append(f"审批链路：{chain_text}")

        return SkillResult(True, "\n".join(lines))