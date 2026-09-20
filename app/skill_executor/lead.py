from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.hermes.crm_mcp_client import CRMMCPError, call_crm_tool
from app.schemas.lead import FollowUpInput, LeadQuery
from app.schemas.skill_context import SkillContext
from app.services.expense.cards import card
from app.services.lead_cards import format_card_date, markdown, query_card
from app.skill_executor.base import BaseSkillExecutor, SkillResult


class LeadSkillExecutor(BaseSkillExecutor):
    async def executor(
        self,
        context: SkillContext,
        slots: dict,
        db: AsyncSession,
    ) -> SkillResult:
        try:
            params = LeadQuery.model_validate(slots)
            arguments = params.model_dump(mode="json")
            result = await call_crm_tool(
                "query_leads",
                int(context.user_id),
                {"params": arguments},
            )
        except ValidationError:
            return SkillResult(False, "查询条件格式不正确，请检查后重试")
        except CRMMCPError as exc:
            return SkillResult(False, str(exc))

        return SkillResult(
            True,
            "线索查询完成",
            data=result,
            card=query_card(result, arguments),
        )


class LeadFollowUpExecutor(BaseSkillExecutor):
    async def executor(
        self,
        context: SkillContext,
        slots: dict,
        db: AsyncSession,
    ) -> SkillResult:
        try:
            values = dict(slots)
            lead_id = int(values.pop("lead_id"))
            if lead_id <= 0:
                raise ValueError

            body = FollowUpInput.model_validate(values)
            result = await call_crm_tool(
                "update_follow_up",
                int(context.user_id),
                {
                    "lead_id": lead_id,
                    "body": body.model_dump(mode="json"),
                },
            )
        except (KeyError, ValueError, ValidationError):
            return SkillResult(False, "跟进信息格式不正确，请检查后重试")
        except CRMMCPError as exc:
            return SkillResult(False, str(exc))

        return SkillResult(
            True,
            "跟进已记录",
            data=result,
            card=card(
                "跟进已记录",
                [markdown(
                    f"线索编号：{lead_id}\n"
                    f"跟进记录编号：{result['id']}\n"
                    f"下次跟进：{format_card_date(result['next_follow_up'], '未安排')}"
                )],
                "green",
            ),
        )
