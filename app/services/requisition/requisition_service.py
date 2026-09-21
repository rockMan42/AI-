import json
import logging
import uuid

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.core import redis_client as redis_module
from app.core.database import create_session
from app.hermes.requisition_mcp_client import (
    RequisitionMCPError,
    call_requisition_tool,
)
from app.models.requisition import Requisition
from app.models.user import User
from app.services.requisition.category_rule_service import (
    CategoryRuleService,
)
from app.utils.time import utc_now


DRAFT_PREFIX = "dep:tmp:requisition_draft"
USER_DRAFT_PREFIX = "dep:tmp:requisition_draft:user"
DRAFT_TTL_SECONDS = 30 * 60
logger = logging.getLogger(__name__)

STATUS_TEXT = {
    "pending": "待审批",
    "approving": "审批中",
    "approved": "已通过",
    "rejected": "已驳回",
    "fulfilled": "已发放",
}

TERMINAL_STATUSES = {
    "approved",
    "rejected",
    "fulfilled",
}


class RequisitionError(ValueError):
    def __init__(
        self,
        message: str,
        status_code: int = 409,
    ):
        super().__init__(message)
        self.status_code = status_code


class OaRequisitionGateway:
    def __init__(self):
        settings = get_settings()
        self.base_url = (
            settings.oa_requisition_base_url.rstrip("/")
        )
        self.timeout = (
            settings.oa_requisition_timeout_seconds
        )

    async def submit(self, payload: dict) -> dict:
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
            ) as client:
                response = await client.post(
                    f"{self.base_url}/api/v1/requisitions",
                    json=payload,
                    headers={
                        "Idempotency-Key":
                            payload["client_request_id"],
                    },
                )
                response.raise_for_status()
                body = response.json()
        except httpx.TimeoutException as exc:
            raise RequisitionError(
                "OA 系统响应超时，请稍后重试",
                503,
            ) from exc
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {400, 422}:
                raise RequisitionError(
                    "OA 拒绝了本次申领信息",
                    422,
                ) from exc
            raise RequisitionError(
                "OA 系统暂不可用，请稍后重试",
                503,
            ) from exc
        except httpx.HTTPError as exc:
            raise RequisitionError(
                "无法连接 OA 系统，请稍后重试",
                503,
            ) from exc

        data = body.get("data")
        if body.get("code") != 0 or not isinstance(data, dict):
            raise RequisitionError("OA 创建申领单失败", 503)

        requisition_id = data.get("requisition_id")
        if (
            type(requisition_id) is not int
            or requisition_id <= 0
        ):
            raise RequisitionError(
                "OA 未返回有效申领单号",
                503,
            )

        return {
            "requisition_id": requisition_id,
            "status": str(
                data.get("status") or "pending"
            ),
            "created_at": data.get("created_at"),
        }

    async def query(self, requisition_id: int) -> dict:
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
            ) as client:
                response = await client.get(
                    f"{self.base_url}/api/v1/requisitions/"
                    f"{requisition_id}",
                )
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPError as exc:
            raise RequisitionError(
                "OA 状态查询失败",
                503,
            ) from exc

        data = body.get("data")
        if body.get("code") != 0 or not isinstance(data, dict):
            raise RequisitionError(
                "OA 未返回有效审批状态",
                503,
            )

        status = str(data.get("status") or "")
        if status not in STATUS_TEXT:
            raise RequisitionError(
                "OA 返回了未知审批状态",
                503,
            )

        data["status_text"] = (
            data.get("status_text")
            or STATUS_TEXT[status]
        )
        return data


class RequisitionService:
    def __init__(self):
        self.rules = CategoryRuleService()

    async def prepare(
        self,
        db: AsyncSession,
        user: User,
        slots: dict,
    ) -> dict:
        category = str(
            slots.get("item_category") or ""
        ).strip()

        rules = await self.rules.get_fields(db, category)
        self.rules.validate(rules, slots)

        draft_id = uuid.uuid4().hex
        draft = {
            "draft_id": draft_id,
            "user_id": user.user_id,
            "item_category": category,
            "item_name": str(slots["item_name"]).strip(),
            "specification": slots.get("specification"),
            "quantity": int(slots.get("quantity", 1)),
            "reason": slots.get("reason"),
            "purpose": slots.get("purpose"),
            "expected_return_date":
                slots.get("expected_return_date"),
            "client_request_id": uuid.uuid4().hex,
            "created_at": utc_now().isoformat(),
        }

        client = redis_module.redis_client
        if client is None:
            raise RequisitionError(
                "Redis 尚未初始化",
                503,
            )

        async with client.pipeline(
            transaction=True,
        ) as pipe:
            pipe.setex(
                f"{DRAFT_PREFIX}:{draft_id}",
                DRAFT_TTL_SECONDS,
                json.dumps(draft, ensure_ascii=False),
            )
            pipe.setex(
                f"{USER_DRAFT_PREFIX}:{user.user_id}",
                DRAFT_TTL_SECONDS,
                draft_id,
            )
            await pipe.execute()

        return draft

    async def get_draft(
        self,
        draft_id: str,
    ) -> dict | None:
        client = redis_module.redis_client
        if client is None:
            raise RequisitionError(
                "Redis 尚未初始化",
                503,
            )

        raw = await client.get(
            f"{DRAFT_PREFIX}:{draft_id}"
        )
        return json.loads(raw) if raw else None

    async def get_user_draft(
        self,
        user_id: int,
    ) -> dict | None:
        client = redis_module.redis_client
        draft_id = await client.get(
            f"{USER_DRAFT_PREFIX}:{user_id}"
        )
        if not draft_id:
            return None
        return await self.get_draft(draft_id)

    async def cancel_draft(
        self,
        user_id: int,
        draft_id: str,
    ) -> None:
        draft = await self.get_draft(draft_id)
        if draft is None:
            raise RequisitionError(
                "申领草稿已过期，请重新填写"
            )
        if draft["user_id"] != user_id:
            raise RequisitionError(
                "不能取消其他员工的申领草稿",
                403,
            )

        client = redis_module.redis_client
        await client.delete(
            f"{DRAFT_PREFIX}:{draft_id}",
            f"{USER_DRAFT_PREFIX}:{user_id}",
        )

    async def submit_draft(
        self,
        db: AsyncSession,
        user: User,
        draft_id: str,
    ) -> dict:
        draft = await self.get_draft(draft_id)
        if draft is None:
            raise RequisitionError(
                "申领草稿已过期，请重新填写"
            )
        if draft["user_id"] != user.user_id:
            raise RequisitionError(
                "不能提交其他员工的申领草稿",
                403,
            )
        user_id = user.user_id

        try:
            result = await call_requisition_tool(
                "submit_requisition",
                {
                    "user_id": user.user_id,
                    "applicant_id": user_id,
                    "item_category":
                        draft["item_category"],
                    "item_name": draft["item_name"],
                    "specification":
                        draft.get("specification"),
                    "quantity": draft["quantity"],
                    "reason": draft.get("reason"),
                    "purpose": draft.get("purpose"),
                    "expected_return_date":
                        draft.get("expected_return_date"),
                    "client_request_id":
                        draft["client_request_id"],
                },
            )
        except RequisitionMCPError as exc:
            # 不删除草稿，用户可发送“重试”。
            logger.exception(
                "requisition_submit_mcp_failed user_id=%s draft_id=%s",
                user_id,
                draft_id,
            )
            raise RequisitionError(
                "申领提交失败，信息已保留，请稍后发送“重试”",
                503,
            ) from exc

        requisition_id = result.get("requisition_id")
        if (
            type(requisition_id) is not int
            or requisition_id <= 0
        ):
            raise RequisitionError(
                "OA 未返回有效申领单号",
                503,
            )

        # 使用独立会话获取 MCP 写库后的最新快照，不回滚调用方会话，
        # 避免其中已加载的 user 等 ORM 对象被标记为过期。
        async with create_session() as sync_db:
            existing = await sync_db.get(
                Requisition,
                requisition_id,
            )
            if existing is None:
                sync_db.add(
                    Requisition(
                        id=requisition_id,
                        user_id=user_id,
                        item_category=draft["item_category"],
                        item_name=draft["item_name"],
                        specification=draft.get(
                            "specification"
                        ),
                        quantity=draft["quantity"],
                        reason=draft.get("reason"),
                        status=result.get(
                            "status",
                            "pending",
                        ),
                    )
                )
                await sync_db.commit()
            elif existing.user_id != user_id:
                raise RequisitionError(
                    "OA 单号与本地其他申请冲突",
                    409,
                )

        from app.services.requisition.approval_poll_service import (
            register_approval_poll,
        )

        await register_approval_poll(
            requisition_id,
            user_id,
            result.get("status", "pending"),
        )

        client = redis_module.redis_client
        await client.delete(
            f"{DRAFT_PREFIX}:{draft_id}",
            f"{USER_DRAFT_PREFIX}:{user_id}",
        )

        return result

    async def retry_latest(
        self,
        db: AsyncSession,
        user: User,
    ) -> dict:
        draft = await self.get_user_draft(user.user_id)
        if draft is None:
            raise RequisitionError(
                "没有可重试的申领草稿"
            )
        return await self.submit_draft(
            db,
            user,
            draft["draft_id"],
        )

    async def latest_mine(
        self,
        db: AsyncSession,
        user_id: int,
    ) -> Requisition | None:
        return await db.scalar(
            select(Requisition)
            .where(Requisition.user_id == user_id)
            .order_by(Requisition.created_at.desc())
            .limit(1)
        )
