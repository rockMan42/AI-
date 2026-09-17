import asyncio
import json
import logging
import re
from uuid import NAMESPACE_URL, uuid4, uuid5

from app.core import redis_client as redis_module
from app.core.redis_client import get_cache, set_cache
from app.schemas.invoice import (
    FIELD_LABELS,
    InvoiceConfirmInput,
    InvoiceOCRInput,
)
from app.services.conversation_engine.feishu import (
    _send_feishu_reply,
    send_feishu_card,
    update_feishu_card,
)
from app.services.conversation_engine.session_store import SessionStore
from app.services.conversation_engine.slot_manage import SlotState
from app.services.expense.cards import invoice_card, summary_card
from app.services.expense.images import store_feishu_image
from app.services.expense.invoice_service import InvoiceError


log = logging.getLogger(__name__)


async def refresh_summary(user_id: int, request_id: str, service):
    """串行读取最新核验结果，刷新本批汇总；失败不影响已提交的核验。"""
    try:
        async with asyncio.timeout(20):
            async with redis_module.redis_client.lock(
                f"dep:lock:ocr-summary:{request_id}",
                timeout=30,
                blocking_timeout=10,
            ):
                saved = await get_cache(f"dep:tmp:ocr-summary:{request_id}")
                if not saved:
                    return
                target = json.loads(saved)
                if target["user_id"] != user_id:
                    return
                batch = await service.detail(user_id, request_id)
                await update_feishu_card(
                    target["message_id"],
                    summary_card(batch, target["upload_failed"]),
                )
    except Exception:
        log.exception("invoice_summary_update_failed request_id=%s", request_id)


def extract_image_keys(
    message_type: str,
    content: dict,
) -> list[str]:
    if message_type == "image":
        keys = [content.get("image_key")]

    elif message_type == "post":
        post = (
            content
            if "content" in content
            else (
                content.get("zh_cn")
                or content.get("en_us")
                or {}
            )
        )

        keys = [
            item.get("image_key")
            for line in post.get("content", [])
            for item in line
            if isinstance(item, dict)
            and item.get("tag") == "img"
        ]
    else:
        keys = []

    return list(
        dict.fromkeys(
            key
            for key in keys
            if isinstance(key, str) and key
        )
    )


async def finish_flow(user_id: int, batch: dict):
    summary = batch["summary"]
    if summary["confirmed_count"] != summary["total_count"]:
        return

    try:
        async with asyncio.timeout(0.3):
            store = SessionStore()
            session = await store.load(str(user_id))
            slot = (
                session.slots.get("ocr_request_id")
                if session
                else None
            )

            if (
                session
                and session.intent_code == "expense_reimburse"
                and slot
                and slot.value == batch["request_id"]
            ):
                session.status = "completed"
                session.state = "completed"
                session.workflow_state = "completed"
                session.pending_slot = None
                await store.save(session)

    except Exception:
        log.warning("invoice_session_finish_failed")


async def handle_invoice_text(
    user_id: int,
    text: str,
    service,
):
    if not text.startswith("修改发票"):
        return None

    lines = text.strip().splitlines()
    match = re.fullmatch(
        r"修改发票\s+([A-Za-z0-9_-]{1,64})\s+([1-9]|10)",
        lines[0],
    )

    if not match or len(lines) < 2:
        raise InvoiceError(
            "请按“需要修改”卡片中的格式发送，每个字段独占一行",
            422,
        )

    names = {
        label: name
        for name, label in FIELD_LABELS.items()
    }
    corrections = {}

    for line in lines[1:]:
        parts = re.split(r"[:：]", line, maxsplit=1)

        if len(parts) != 2 or parts[0].strip() not in names:
            raise InvoiceError(
                "修改字段名称不正确，请使用卡片上的中文名称",
                422,
            )

        name = names[parts[0].strip()]
        if name in corrections:
            raise InvoiceError(
                "同一字段不能重复填写",
                422,
            )

        value = parts[1].strip()
        corrections[name] = (
            None if value == "不适用" else value
        )

    body = InvoiceConfirmInput(
        request_id=match[1],
        index=int(match[2]),
        corrections=corrections,
    )

    batch = await service.confirm(user_id, body)
    await finish_flow(user_id, batch)
    await refresh_summary(user_id, body.request_id, service)

    item = next(
        item
        for item in batch["results"]
        if item["index"] == body.index
    )
    return invoice_card(body.request_id, item)


class InvoiceChat:
    def __init__(self, service):
        self.service = service
        self._pending = {}
        self._tasks = {}
        self._downloads = asyncio.Semaphore(3)

    def enqueue(
        self,
        user,
        message_id: str,
        keys: list[str],
        flow_key: float,
    ):
        queue = self._pending.setdefault(user.user_id, [])
        queue.extend(
            (message_id, key, flow_key)
            for key in keys
        )

        if user.user_id not in self._tasks:
            self._tasks[user.user_id] = asyncio.create_task(
                self._run(user)
            )

    async def close(self):
        if self._tasks:
            await asyncio.gather(
                *list(self._tasks.values()),
                return_exceptions=True,
            )

    async def _upload(
        self,
        user_id,
        message_id,
        key,
    ):
        async with self._downloads:
            async with asyncio.timeout(45):
                return await store_feishu_image(
                    user_id,
                    message_id,
                    key,
                )

    async def _run(self, user):
        try:
            while self._pending.get(user.user_id):
                await asyncio.sleep(0.8)

                queue = self._pending[user.user_id]
                flow_key = queue[0][2]
                jobs = []

                while (
                    queue
                    and len(jobs) < 10
                    and queue[0][2] == flow_key
                ):
                    jobs.append(queue.pop(0))

                try:
                    await self._process(user, jobs, flow_key)

                except Exception as exc:
                    log.error(
                        "invoice_chat_failed error_type=%s",
                        type(exc).__name__,
                    )

                    try:
                        await _send_feishu_reply(
                            jobs[0][0],
                            "发票处理未完成，请稍后重新上传。",
                        )
                    except Exception:
                        log.warning(
                            "invoice_failure_reply_failed"
                        )
        finally:
            self._pending.pop(user.user_id, None)
            self._tasks.pop(user.user_id, None)

    async def _process(self, user, jobs, flow_key):
        uploads = await asyncio.gather(
            *(
                self._upload(
                    user.user_id,
                    message_id,
                    key,
                )
                for message_id, key, _ in jobs
            ),
            return_exceptions=True,
        )

        urls = []

        for job, result in zip(jobs, uploads):
            if isinstance(result, Exception):
                await _send_feishu_reply(
                    job[0],
                    "这张图片接收失败，请重新上传清晰图片。",
                )
            else:
                urls.append(result)

        request_id = uuid4().hex

        if urls:
            batch = await self.service.recognize(
                user.user_id,
                InvoiceOCRInput(
                    user_id=user.user_id,
                    request_id=request_id,
                    image_urls=urls,
                ),
            )

            store = SessionStore()
            session = await store.load(str(user.user_id))

            if (
                session
                and session.intent_code == "expense_reimburse"
                and session.intent_started_at == flow_key
            ):
                session.slots["ocr_request_id"] = SlotState(
                    name="ocr_request_id",
                    value=request_id,
                    filled=True,
                )
                session.status = "awaiting_confirmation"
                session.state = "matched"
                session.workflow_state = "matched"
                session.pending_slot = None
                await store.save(session)

            for item in batch["results"]:
                await send_feishu_card(
                    user.feishu_open_id,
                    invoice_card(request_id, item),
                    uuid5(
                        NAMESPACE_URL,
                        f"{request_id}:{item['index']}",
                    ).hex,
                )
        else:
            batch = {
                "request_id": request_id,
                "results": [],
                "summary": {
                    "total_count": 0,
                    "confirmed_count": 0,
                    "needs_confirm_count": 0,
                },
            }

        message_id = await send_feishu_card(
            user.feishu_open_id,
            summary_card(batch, len(jobs) - len(urls)),
            uuid5(
                NAMESPACE_URL,
                f"{request_id}:summary",
            ).hex,
        )
        await set_cache(
            f"dep:tmp:ocr-summary:{request_id}",
            json.dumps({
                "user_id": user.user_id,
                "message_id": message_id,
                "upload_failed": len(jobs) - len(urls),
            }),
            expire=14 * 24 * 60 * 60,
        )
        if urls:
            await refresh_summary(user.user_id, request_id, self.service)
