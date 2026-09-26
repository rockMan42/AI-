import logging

from fastapi import APIRouter, Depends, HTTPException

from app.hermes.finance_mcp_client import (
    FinanceMCPError,
    call_finance_tool,
)
from app.models import User
from app.schemas.expense import (
    ExpenseCheckInput,
    ExpensePolicyInput,
    ExpensePrepareInput,
    ExpenseRuleInput,
    ExpenseSubmitInput,
    ExpenseSupplement,
)
from app.services.expense.approval_service import approval_detail
from app.security.auth import get_current_user, require_admin
from app.services.expense.expense_service import ExpenseService
from app.services.expense.rules import ExpenseError
from app.services.business_rules.common import RuleError
from app.schemas.permission import AccessDenied, PermissionUnavailable
from app.services.expense.approval_mock import (
    MockApprovalInput,
    advance_mock,
)

router = APIRouter()
service = ExpenseService()
log = logging.getLogger(__name__)


async def invoke(operation):
    try:
        return {"code": 200, "data": await operation}
    except (ExpenseError, RuleError) as exc:
        raise HTTPException(exc.status_code, str(exc)) from None
    except (AccessDenied, PermissionUnavailable):
        # 由应用统一处理权限错误，避免兼容管理入口把 403 包装成 503。
        raise
    except FinanceMCPError:
        raise HTTPException(503, "财务服务暂不可用") from None
    except Exception:
        log.exception("expense_api_failed")
        raise HTTPException(503, "报销服务暂不可用") from None


@router.post("/admin/expense/{expense_id}/mock-approval")
async def mock_approval(
    expense_id: int,
    body: MockApprovalInput,
    user: User = Depends(require_admin),
):
    return await invoke(
        advance_mock(user.user_id, expense_id, body)
    )

@router.put("/expense/invoices/{invoice_id}/supplement")
async def supplement(
    invoice_id: int,
    body: ExpenseSupplement,
    user: User = Depends(get_current_user),
):
    """
    补充发票信息
    :param invoice_id:
    :param body:
    :param user:
    :return:
    """
    return await invoke(
        service.supplement(user.user_id, invoice_id, body)
    )

@router.get("/expense/trips")
async def trips(user: User = Depends(get_current_user)):
    """
    用户出差信息
    :param user:
    :return:
    """
    return await invoke(service.trips(user.user_id))

@router.post("/expense/rule/check")
async def check(
    body: ExpenseCheckInput,
    user: User = Depends(get_current_user),
):
    """
    报销单审核
    :param body:
    :param user:
    :return:
    """
    return await invoke(service.check(user.user_id, body))


@router.post("/expense/prepare")
async def prepare(
    body: ExpensePrepareInput,
    user: User = Depends(get_current_user),
):
    """
    报销单准备
    :param body:
    :param user:
    :return:
    """
    return await invoke(service.prepare(user.user_id, body))

@router.post("/expense/submit")
async def submit(
    body: ExpenseSubmitInput,
    user: User = Depends(get_current_user),
):
    """
    报销单提交
    :param body:
    :param user:
    :return:
    """
    return await invoke(
        service.submit(user.user_id, body.expense_id)
    )

@router.post("/expense/{expense_id}/cancel")
async def cancel(
    expense_id: int,
    user: User = Depends(get_current_user),
):
    """
    报销单取消
    :param expense_id:
    :param user:
    :return:
    """
    return await invoke(service.cancel(user.user_id, expense_id))

@router.get("/expense/{expense_id}")
async def detail(
    expense_id: int,
    user: User = Depends(get_current_user),
):
    """
    报销单详情
    :param expense_id:
    :param user:
    :return:
    """
    return await invoke(service.detail(user.user_id, expense_id))

@router.get("/expense/{expense_id}/finance")
async def finance(
    expense_id: int,
    user: User = Depends(get_current_user),
):
    """
    调用财务mcp查询报销单信息
    :param expense_id:
    :param user:
    :return:
    """
    async def query():
        await service.detail(user.user_id, expense_id)
        return await call_finance_tool(
            "query_expense", user.user_id, expense_id,
        )

    return await invoke(query())

@router.post("/admin/expense-rules")
async def create_rule(
    body: ExpenseRuleInput,
    user: User = Depends(get_current_user),
):
    return await invoke(service.save_rule(user.user_id, None, body))


@router.put("/admin/expense-rules/{rule_id}")
async def update_rule(
    rule_id: int,
    body: ExpenseRuleInput,
    user: User = Depends(get_current_user),
):
    return await invoke(service.save_rule(user.user_id, rule_id, body))


@router.get("/admin/expense-policy")
async def get_policy(user: User = Depends(get_current_user)):
    return await invoke(service.get_policy(user.user_id))

@router.put("/admin/expense-policy")
async def update_policy(
    body: ExpensePolicyInput,
    user: User = Depends(get_current_user),
):
    return await invoke(service.save_policy(user.user_id, body))

@router.get("/expense/{expense_id}/approval-status")
async def approval_status(
    expense_id: int,
    user: User = Depends(get_current_user),
):
    return await invoke(
        approval_detail(user.user_id, expense_id)
    )


@router.post("/expense/{expense_id}/reopen")
async def reopen(
    expense_id: int,
    user: User = Depends(get_current_user),
):
    return await invoke(
        service.reopen_rejected(user.user_id, expense_id)
    )


from typing import Literal

from app.config.settings import get_settings
from app.services.expense.approval_cron import (
    execute_expense_task,
    reconcile_tasks,
)


@router.post("/admin/expense-test/run/{kind}")
async def run_expense_test_task(
    kind: Literal["poll", "notify", "timeout"],
    user: User = Depends(require_admin),
):
    if not get_settings().expense_mock_enabled:
        raise HTTPException(404, "测试入口未开启")

    task_ids = {
        "poll": "EXPENSE_APPROVAL_POLL",
        "notify": "EXPENSE_NOTIFICATION_DELIVERY",
        "timeout": "EXPENSE_TIMEOUT_SCAN",
    }

    async def operation():
        await reconcile_tasks()
        result = await execute_expense_task(
            task_ids[kind],
            force=True,
        )
        if result.get("status") != "PENDING":
            raise ExpenseError("测试任务执行失败，请检查日志", 503)
        return result

    return await invoke(operation())
