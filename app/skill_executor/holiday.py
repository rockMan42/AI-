from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.holiday import NoticeInput
from app.schemas.skill_context import SkillContext
from app.schemas.skill_result import SkillResult
from app.services import holiday as service
from app.services.conversation_engine.session_store import SessionStore
from app.services.holiday_cron import HolidayError
from app.services.holiday_cards import notice_list_card, notice_stats_card
from app.skill_executor.base import BaseSkillExecutor


class HolidayNoticeExecutor(BaseSkillExecutor):
    async def executor(self,context: SkillContext, slots: dict, db: AsyncSession) -> SkillResult:
        """
        将对话中收集到的信息转成一张HR待确认的卡片
        :param context:
        :param slots:
        :param db:
        :return:
        """
        try:
            body = NoticeInput.model_validate(slots) # 将对话殷勤中的信息转换成NoticeInput
            session = await SessionStore().load(context.session_id) # 从Redis获取本次会话

            if session is None:
                return SkillResult(False, "会话已过期，请重新开始")

            message_card = await service.prepare_notice(context.open_id, body, str(session.intent_started_at))

            """
                后续只有HR点击创建才会走：
                卡片回调
                → confirm_notice_draft()
                → create_notice()
                → 写入 t_holiday_notice
                → 注册 Cron 推送任务
            """
            return SkillResult(True, "请确认放假安排",data={"awaiting_confirmation": True},card = message_card)
        except ValidationError:
            return SkillResult(False, "请检查放假日期，值班安排及带时区的截止时间")
        except HolidayError as exc:
            return SkillResult(False,str(exc))

class ReceiptConfirmExecutor(BaseSkillExecutor):
    async def executor(self,context: SkillContext, slots: dict, db: AsyncSession) -> SkillResult:
        """
        员工用来接收确认的执行器,员工确认收到放假通知
        :param context:
        :param slots:
        :param db:
        :return:
        """
        try:
            notice, _ = await service.confirm_receipt(context.open_id,slots.get("notice_id"))
            return SkillResult(
                True,
                (
                    f"✅ 已确认收到【{notice.holiday_name}】放假通知"
                    if notice else "您当前没有待确认的通知"
                ),
            )
        except HolidayError as exc:
            return SkillResult(False, str(exc))

class HolidayQueryExecutor(BaseSkillExecutor):
    async def executor(self,context: SkillContext, slots: dict, db: AsyncSession) -> SkillResult:
        """
        员工查询放假通知
        :param context:
        :param slots:
        :param db:
        :return:
        """
        try:
            notice_id = slots.get("notice_id")
            if notice_id is None:
                result = await service.list_notices(context.open_id,keyword=slots.get("keyword"),limit=5)
                if not result["items"]:
                    return SkillResult(False, "没有符合条件的通知")
                return SkillResult(
                    True, "请选择要查看的通知",
                    card=notice_list_card(result["items"], result["has_more"],
                                          keyword=slots.get("keyword")),
                )

            notice = await service.detail(context.open_id, notice_id)
            stats = await service.receipt_stats(context.open_id, notice_id)

            return SkillResult(
                True,
                "通知回执统计已更新",
                card=notice_stats_card(notice, stats),
            )
        except HolidayError as exc:
            return SkillResult(False, str(exc))
