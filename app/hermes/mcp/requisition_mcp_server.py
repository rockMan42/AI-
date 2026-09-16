import json
import logging
import sys
from contextlib import asynccontextmanager, AsyncExitStack

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from sqlalchemy import select
from app.core import redis_client as redis_module
from app.config.settings import get_settings
from app.core.database import init_db, close_db, create_session
from app.core.redis_client import init_redis, close_redis
from app.models import User, Requisition, RequisitionApproval
from app.services.requisition.category_rule_service import CategoryRuleService
from app.services.requisition.requisition_service import OaRequisitionGateway, RequisitionError, STATUS_TEXT


@asynccontextmanager
async def lifespan(server: FastMCP):
    settings = get_settings()

    async with AsyncExitStack() as stack:
        await init_db(settings)
        stack.push_async_callback(close_db)

        await init_redis(settings)
        stack.push_async_callback(close_redis)

        yield {
            "gateway": OaRequisitionGateway(), # 封装OA接口
            "rules": CategoryRuleService(), # 封装品类规则
        }



mcp_server = FastMCP(
    name="oa_material_requisition_mcp",
    lifespan=lifespan
)


@mcp_server.tool(name="list_categories")
async def list_categories(ctx: Context) -> dict:
    """
    查看可申领的物资品类
    :param ctx:
    :return:
    """
    service = ctx.request_context.lifespan_context["rules"]

    async with create_session() as db:
        return {"categories": await service.list_categories(db)}

@mcp_server.tool(name="get_category_fields")
async def get_category_fields(category: str, ctx: Context) -> dict:
    service = ctx.request_context.lifespan_context["rules"]

    try:
        async with create_session() as db:
            fields = await service.get_fields(db, category)
            return {"category": category, "fields": fields}
    except ValueError as exc:
        raise ToolError(str(exc)) from None


@mcp_server.tool(name="submit_requisition")
async def submit_requisition(
    applicant_id: int,
    item_category: str,
    item_name: str,
    quantity: int,
    client_request_id: str,
    ctx: Context,
    specification: str | None = None,
    reason: str | None = None,
    purpose: str | None = None,
    expected_return_date: str | None = None,
) -> dict:
    """本地模式直接创建申领单；HTTP 模式调用真实 OA。"""

    if applicant_id <= 0:
        raise ToolError("申请人无效")
    if not item_category.strip():
        raise ToolError("物资品类不能为空")
    if not item_name.strip():
        raise ToolError("物资名称不能为空")
    if not 1 <= quantity <= 100:
        raise ToolError("数量必须在 1-100 之间")
    if (
        not client_request_id
        or len(client_request_id) > 64
    ):
        raise ToolError("幂等请求号无效")

    settings = get_settings()

    if settings.oa_requisition_mode == "http":
        gateway = ctx.request_context.lifespan_context["gateway"]

        try:
            return await gateway.submit(
                {
                    "applicant_id": applicant_id,
                    "item_category": item_category,
                    "item_name": item_name,
                    "specification": specification,
                    "quantity": quantity,
                    "reason": reason,
                    "purpose": purpose,
                    "expected_return_date":
                        expected_return_date,
                    "client_request_id":
                        client_request_id,
                }
            )
        except RequisitionError as exc:
            raise ToolError(str(exc)) from None

    client = redis_module.redis_client
    idempotency_key = (
        "dep:mock:oa:idempotency:"
        f"{client_request_id}"
    )

    # 重复提交时返回第一次创建的申领单。
    cached = await client.get(idempotency_key)
    if cached:
        return json.loads(cached)

    async with create_session() as db:
        applicant = await db.get(User, applicant_id)
        if applicant is None:
            raise ToolError("申请人不存在")

        requisition = Requisition(
            user_id=applicant_id,
            item_category=item_category.strip(),
            item_name=item_name.strip(),
            specification=specification,
            quantity=quantity,
            reason=reason,
            status="pending",
            approver_id=None,
        )
        db.add(requisition)
        await db.flush()
        await db.refresh(
            requisition,
            attribute_names=["created_at"],
        )

        result = {
            "requisition_id": requisition.id,
            "status": requisition.status,
            "created_at":
                requisition.created_at.isoformat()
                if requisition.created_at
                else None,
        }

        # 先保存数据库，再保存幂等映射。
        await db.commit()

    await client.setex(
        idempotency_key,
        30 * 24 * 60 * 60,
        json.dumps(result, ensure_ascii=False),
    )
    return result

@mcp_server.tool(name="query_requisition")
async def query_requisition(
    requisition_id: int,
    ctx: Context,
) -> dict:
    """本地模式查询数据库；HTTP 模式查询真实 OA。"""

    if requisition_id <= 0:
        raise ToolError("申领单号无效")

    settings = get_settings()

    if settings.oa_requisition_mode == "http":
        gateway = ctx.request_context.lifespan_context["gateway"]
        try:
            return await gateway.query(requisition_id)
        except RequisitionError as exc:
            raise ToolError(str(exc)) from None

    async with create_session() as db:
        requisition = await db.get(
            Requisition,
            requisition_id,
        )
        if requisition is None:
            raise ToolError("申领单不存在")

        approval_rows = (
            await db.scalars(
                select(RequisitionApproval)
                .where(
                    RequisitionApproval.requisition_id
                    == requisition_id
                )
                .order_by(
                    RequisitionApproval.created_at
                )
            )
        ).all()

        approval_chain = []
        for approval in approval_rows:
            approver = await db.get(
                User,
                approval.approver_id,
            )
            approval_chain.append(
                {
                    "approver_id":
                        approval.approver_id,
                    "approver_name": (
                        approver.name
                        if approver
                        else "审批人"
                    ),
                    "action": approval.action,
                    "action_time":
                        approval.created_at.isoformat(),
                    "comment": approval.comment,
                }
            )

        current_approver = None
        if requisition.approver_id:
            approver = await db.get(
                User,
                requisition.approver_id,
            )
            current_approver = {
                "approver_id":
                    requisition.approver_id,
                "approver_name": (
                    approver.name
                    if approver
                    else "审批人"
                ),
            }

        return {
            "requisition_id": requisition.id,
            "item_category":
                requisition.item_category,
            "item_name": requisition.item_name,
            "specification":
                requisition.specification,
            "quantity": requisition.quantity,
            "status": requisition.status,
            "status_text": STATUS_TEXT.get(
                requisition.status,
                requisition.status,
            ),
            "approval_chain": approval_chain,
            "current_approver": current_approver,
            "created_at":
                requisition.created_at.isoformat(),
            "updated_at":
                requisition.updated_at.isoformat(),
        }

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    mcp_server.run(transport="stdio")
