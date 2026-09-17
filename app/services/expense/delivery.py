

"""
持久化提交任务与通知
"""


import asyncio
import logging
from datetime import timedelta
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import or_, select

from app.core.database import create_session
from app.hermes.finance_mcp_client import call_finance_tool
from app.models import Expense, User
from app.services.conversation_engine.feishu import send_feishu_card
from app.services.expense.cards import card
from app.utils.time import utc_now


log = logging.getLogger(__name__)


class ExpenseDelivery:
    def __init__(self):
        self._task = None
        self._stop = asyncio.Event()

    async def start(self):
        """
        应用启动时创建任务
        :return:
        """
        self._stop.clear()
        self._task = asyncio.create_task(self._run())

    async def close(self):
        """
        应用关闭时取消任务
        :return:
        """
        self._stop.set()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _deliver(self, expense_id):
        """
        调用财务MCP提交待提交的报销单
        :param expense_id:
        :return:
        """
        async with create_session() as db:
            row = await db.get(Expense, expense_id)
            if row is None or row.status != "submitting":
                return
            user_id = row.user_id

        try:
            # 即使上次已被财务接收但本地没收到响应，
            # 再次调用也返回同一财务单。
            result = await call_finance_tool(
                "submit_expense", user_id, expense_id,
            )
            if (
                result.get("expense_id") != expense_id
                or not isinstance(result.get("finance_no"), str)
                or not result["finance_no"]
                or result.get("status") not in {
                    "pending_manager", "pending_finance",
                }
            ):
                raise ValueError("财务响应不完整")

            async with create_session() as db, db.begin():
                row = await db.scalar(
                    select(Expense)
                    .where(Expense.id == expense_id)
                    .with_for_update()
                )
                if row.status != "submitting":
                    return
                row.status = "submitted"
                row.finance_no = result["finance_no"]
                row.submitted_at = utc_now()
                row.next_attempt_at = None
                row.last_error = None

        except Exception:
            log.exception("expense_delivery_failed expense_id=%s", expense_id)
            async with create_session() as db, db.begin():
                row = await db.scalar(
                    select(Expense)
                    .where(Expense.id == expense_id)
                    .with_for_update()
                )
                if row is None or row.status != "submitting":
                    return
                row.submit_attempts += 1
                delay = min(300, 2 ** min(row.submit_attempts, 8))
                row.next_attempt_at = utc_now() + timedelta(seconds=delay)
                row.last_error = "财务接收结果尚未确认，系统正在重试"

    async def _notify(self, expense_id):
        """
        通知用户报销单已提交
        :param expense_id:
        :return:
        """
        async with create_session() as db:
            row = await db.get(Expense, expense_id)
            if row is None or row.status != "submitted" or row.notified:
                return
            user = await db.get(User, row.user_id)
            if user is None:
                return
            open_id = user.feishu_open_id
            content = (
                f"报销单已提交：{row.expense_no}\n"
                f"财务单号：{row.finance_no}\n"
                f"金额：{row.total_amount} 元\n"
                + (
                    "本单包含超标申请，已进入主管审批队列。"
                    if row.has_override
                    else "已进入财务审核队列。"
                )
            )

        await send_feishu_card(
            open_id,
            card(
                "报销提交成功",
                [{"tag": "markdown", "content": content}],
                "green",
            ),
            uuid5(
                NAMESPACE_URL,
                f"expense-submitted:{expense_id}",
            ).hex,
        )

        async with create_session() as db, db.begin():
            row = await db.get(Expense, expense_id)
            row.notified = True

    async def _run(self):
        """
        不断的检查并处理待提交的报销单
        :return:
        """
        while not self._stop.is_set():
            try:
                async with create_session() as db:
                    pending = list((
                        await db.scalars(
                            select(Expense.id)
                            .where(
                                Expense.status == "submitting",
                                or_(
                                    Expense.next_attempt_at.is_(None),
                                    Expense.next_attempt_at <= utc_now(),
                                ),
                            )
                            .order_by(Expense.id)
                            .limit(20)
                        )
                    ).all())

                    notifications = list((
                        await db.scalars(
                            select(Expense.id)
                            .where(
                                Expense.status == "submitted",
                                Expense.notified.is_(False),
                                Expense.expense_no.is_not(None),
                            )
                            .order_by(Expense.id)
                            .limit(20)
                        )
                    ).all())

                for expense_id in pending:
                    await self._deliver(expense_id)

                for expense_id in notifications:
                    try:
                        await self._notify(expense_id)
                    except Exception:
                        log.exception(
                            "expense_notification_failed expense_id=%s",
                            expense_id,
                        )

            except Exception:
                log.exception("expense_delivery_loop_failed")

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=2)
            except TimeoutError:
                pass