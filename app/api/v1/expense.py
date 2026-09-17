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
from app.security.auth import get_current_user, require_admin
from app.services.expense.expense_service import ExpenseService
from app.services.expense.rules import ExpenseError

router = APIRouter()
service = ExpenseService()
log = logging.getLogger(__name__)


async def invoke(operation):
    try:
        return {"code": 200, "data": await operation}
    except ExpenseError as exc:
        raise HTTPException(exc.status_code, str(exc)) from None
    except FinanceMCPError:
        raise HTTPException(503, "财务服务暂不可用") from None
    except Exception:
        log.exception("expense_api_failed")
        raise HTTPException(503, "报销服务暂不可用") from None

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
    user: User = Depends(require_admin),
):
    return await invoke(service.save_rule(user.user_id, None, body))


@router.put("/admin/expense-rules/{rule_id}")
async def update_rule(
    rule_id: int,
    body: ExpenseRuleInput,
    user: User = Depends(require_admin),
):
    return await invoke(service.save_rule(user.user_id, rule_id, body))


@router.get("/admin/expense-policy")
async def get_policy(user: User = Depends(require_admin)):
    return await invoke(service.get_policy())

@router.put("/admin/expense-policy")
async def update_policy(
    body: ExpensePolicyInput,
    user: User = Depends(require_admin),
):
    return await invoke(service.save_policy(user.user_id, body))



