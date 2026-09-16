import logging
from typing import Annotated
from app.config.settings import get_settings
from app.schemas.requisition import MockStatusInput
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.models.requisition import (
    Requisition,
    RequisitionApproval,
)
from app.models.user import User
from app.schemas.requisition import (
    CategoryRuleReplaceInput,
)
from app.security.auth import get_current_user, require_admin
from app.services.requisition.approval_poll_service import (
    execute_approval_poll,
    poll_task_id,
    register_approval_poll,
)
from app.services.requisition.category_rule_service import (
    CategoryRuleError,
    CategoryRuleService,
)
from app.services.requisition.requisition_service import (
    STATUS_TEXT,
)


log = logging.getLogger(__name__)
router = APIRouter()
Actor = Annotated[User, Depends(get_current_user)]
DB = Annotated[AsyncSession, Depends(get_session)]





@router.get("/material-categories")
async def list_categories(
        actor: Actor,
        db: DB
):
    return {
        "code": 0,
        "data": await CategoryRuleService().list_categories(db)
    }


@router.get("/material-categories/{category}/fields")
async def category_fields(
        category: str,
        actor: Actor,
        db: DB
):
    try:
        fields = await CategoryRuleService().get_fields(db,category)

    except CategoryRuleError as exc:
        raise HTTPException(404,str(exc)) from None

    return {
        "code": 0,
        "data":{
            "category": category,
            "fields": fields
        }
    }

@router.put("/category-field-rules/{category}")
async def replace_rules(
        category: str,
        body: CategoryRuleReplaceInput,
        admin: Annotated[User, Depends(require_admin)],
        db: DB
):
    try:
        fields = [
            item.model_dump()
            for item in body.fields
        ]
        await CategoryRuleService().replace_category_rules(db,category,fields)
    except CategoryRuleError as exc:
        raise HTTPException(404,str(exc)) from None

    return {
        "code": 0,
        "data": {
            "category":category
        }
    }

@router.get("/requisitions")
async def list_requisition(
        actor: Actor,
        db: DB,
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=10,ge=1,le=50)
):
    condition = Requisition.user_id == actor.user_id

    total = await db.scalar(
        select(func.count())
        .select_from(Requisition)
        .where(condition)
    )

    rows = (
        await db.scalars(
            select(Requisition)
            .where(condition)
            .order_by(Requisition.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()

    return {
        "code": 0,
        "data":{
            "total": total or 0,
            "items":[
                {
                    "requisition_id": row.id,
                    "item_category": row.item_category,
                    "item_name": row.item_name,
                    "quantity": row.quantity,
                    "status": row.status,
                    "status_text": STATUS_TEXT.get(
                        row.status,
                        row.status,
                    ),
                    "created_at":
                        row.created_at.isoformat(),
                }
                for row in rows
            ]
        }
    }

@router.get("/requisitions/{requisition_id}")
async def requisition_detail(requisition_id: int, actor: Actor, db: DB):
    row = await db.get(Requisition, requisition_id)
    if row is None:
        raise HTTPException(404,"申领单不存在")

    if row.user_id != actor.user_id and actor.role not in {"admin","manager","管理员","经理"}:
        raise HTTPException(403, "无权查看此申领单")

    approvals = (
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


    return {
        "code": 0,
        "data": {
            "requisition_id": row.id,
            "item_category": row.item_category,
            "item_name": row.item_name,
            "specification": row.specification,
            "quantity": row.quantity,
            "reason": row.reason,
            "status": row.status,
            "status_text": STATUS_TEXT.get(
                row.status,
                row.status,
            ),
            "current_approver_id": row.approver_id,
            "approval_chain": [
                {
                    "approver_id": item.approver_id,
                    "action": item.action,
                    "comment": item.comment,
                    "action_time":
                        item.created_at.isoformat(),
                }
                for item in approvals
            ],
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
        },
    }


@router.post("/requisitions/{requisition_id}/refresh")
async def refresh_requisition(
    requisition_id: int,
    actor: Actor,
    db: DB,
):
    row = await db.get(Requisition, requisition_id)
    if row is None:
        raise HTTPException(404, "申领单不存在")
    if row.user_id != actor.user_id:
        raise HTTPException(403, "无权刷新此申领单")

    await register_approval_poll(
        row.id,
        row.user_id,
        row.status,
    )
    result = await execute_approval_poll(
        poll_task_id(row.id),
        force=True,
    )
    return {"code": 0, "data": result}




@router.put(
    "/requisitions/{requisition_id}/mock-status"
)
async def mock_requisition_status(
    requisition_id: int,
    body: MockStatusInput,
    actor: Actor,
    db: DB,
):
    """
    仅本地联调使用。
    真实 OA 接入后必须关闭 REQUISITION_MOCK_ENABLED。
    """
    if not get_settings().requisition_mock_enabled:
        raise HTTPException(
            404,
            "接口不存在",
        )

    requisition = await db.get(
        Requisition,
        requisition_id,
    )
    if requisition is None:
        raise HTTPException(404, "申领单不存在")

    # 只有本人或管理员可以操作本地模拟状态。
    # 单账号联调时本人即可使用。
    if (
        requisition.user_id != actor.user_id
        and actor.role not in {"admin", "管理员"}
    ):
        raise HTTPException(403, "无权模拟该申领单")

    if body.approver_id is not None:
        approver = await db.get(
            User,
            body.approver_id,
        )
        if approver is None:
            raise HTTPException(
                422,
                "指定审批人不存在",
            )

    requisition.status = body.status
    requisition.approver_id = (
        None
        if body.status
        in {"approved", "rejected", "fulfilled"}
        else body.approver_id
    )

    if body.action is not None:
        if body.approver_id is None:
            raise HTTPException(
                422,
                "记录审批动作时必须提供 approver_id",
            )

        db.add(
            RequisitionApproval(
                requisition_id=requisition.id,
                approver_id=body.approver_id,
                action=body.action,
                comment=body.comment,
            )
        )

    await db.commit()

    return {
        "code": 0,
        "data": {
            "requisition_id": requisition.id,
            "status": requisition.status,
            "approver_id": requisition.approver_id,
        },
    }




