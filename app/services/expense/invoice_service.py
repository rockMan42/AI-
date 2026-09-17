import asyncio
from contextlib import asynccontextmanager
import json
from sqlalchemy import select

from app.core import redis_client as redis_module
from app.core.database import create_session
from app.core.redis_client import set_cache
from app.models.expense import Expense, ExpenseItem, Invoice
from app.schemas.invoice import (
    FIELD_LABELS,
    InvoiceConfirmInput,
    InvoiceOCRInput,
)
from app.services.expense.images import (
    load_image,
    object_key,
    prepare_image,
)
from app.services.expense.invoice_ocr import (
    failed_result,
    normalize_value,
)
from app.utils.time import utc_now


class InvoiceError(ValueError):
    def __init__(self,
                message: str,
                status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code



@asynccontextmanager
async def request_lock(request_id: str):
    lock = redis_module.redis_client.lock(
        f"dep:lock:ocr:{request_id}",
        timeout=180,
        blocking_timeout=0.2,
    )

    if not await lock.acquire():
        raise InvoiceError("本批发票正在处理, 请稍后重试")

    try:
        async with asyncio.timeout(150):
            yield
    finally:
        if await lock.owned():
            await lock.release()




async def load_rows(db, user_id: int, request_id: str):
    """
    加载本批发票
    :param db:
    :param user_id:
    :param request_id:
    :return:
    """
    rows = list(
        (
            await db.scalars(
                select(Invoice)
                .where(
                    Invoice.ocr_result_json["_meta"]["request_id"]
                    .as_string() == request_id
                )
                .with_for_update()
            )
        ).all()
    )

    if any(
        row.ocr_result_json["_meta"]["user_id"] != user_id
        for row in rows
    ):
        raise InvoiceError("本批发票不属于当前用户", 403)

    return sorted(
        rows,
        key=lambda row: row.ocr_result_json["_meta"]["index"]
    )


def pack_batch(request_id: str, rows: list[Invoice]) -> dict:
    """
    打包批量发票（数据转换）
    :param request_id:
    :param rows:
    :return:
    """
    results = [
        {
            **{
                key: value
                for key, value in row.ocr_result_json.items()
                if key != "_meta"
            },
            "invoice_id": row.id,
            "image_url": row.image_url,
            "index": row.ocr_result_json["_meta"]["index"],
            "verified": bool(row.verified),
        }
        for row in rows
    ]

    return {
        "request_id": request_id,
        "results": results,
        "summary": {
            "total_count": len(results),
            "confirmed_count": sum(
                item["verified"] for item in results
            ),
            "needs_confirm_count": sum(
                item["needs_confirm"] for item in results
            ),
        },
    }

async def cache_batch(user_id: int, batch: dict):
    """
    缓存批量发票结果
    :param user_id:
    :param batch:
    :return:
    """
    await set_cache(
        f"dep:tmp:ocr:{batch['request_id']}",
        json.dumps(
            {
                "user_id": user_id,
                **batch
            },
            ensure_ascii=False
        ),
        expire=600
    )


def validated_corrections(values: dict) -> dict:
    """"
    校验修正值
    """
    if set(values) - FIELD_LABELS.keys():
        raise InvoiceError("包含不允许修改的字段", 422)

    result = {}

    for name, value in values.items():
        normalized = normalize_value(name, value)
        if value is not None and normalized is None:
            raise InvoiceError(f"字段 {name} 的值 {value} 不合法", 422)

        if isinstance(normalized, str) and len(normalized) > 500:
            raise InvoiceError(f"字段 {name} 的值 {value} 长度超过 500", 422)

        result[name] = normalized

    return result


class InvoiceService:
    def __init__(self, ocr):
        self.ocr = ocr
        self._images = asyncio.Semaphore(3)


    async def _recognize(self, image_url: str, user_id: int):
        async with self._images:
            try:
                data = await load_image(image_url, user_id)
                _, _, source = await asyncio.to_thread(
                    prepare_image,
                    data
                )
            except PermissionError:
                raise
            except Exception:
                return (
                    failed_result("图片读取失败，请重新上传"),
                    False
                )

            return await self.ocr.recognize(source), True

    async def recognize(self, user_id: int, body: InvoiceOCRInput) -> dict:
            """
            发票识别
            :param user_id:
            :param body:
            :return:
            """
            if body.user_id != user_id:
                raise InvoiceError("本批发票不属于当前用户")

            for url in body.image_urls:
                object_key(url,user_id)

            async with request_lock(body.request_id):
                async with create_session() as db:
                    rows = await load_rows(db, user_id, body.request_id)

                    if rows and [
                        row.image_url
                        for row in rows
                    ] != body.image_urls:
                        raise InvoiceError("同一请求编号不能更换招牌呢", 409)

                if not rows:
                    results = await asyncio.gather(
                        *[
                            self._recognize(url, user_id)
                            for url in body.image_urls
                        ]
                    )

                    async with create_session() as db:
                        rows = []

                        for index, (url, recognized) in enumerate(zip(body.image_urls, results), start=1):
                            result, image_ready = recognized
                            payload = result.model_dump(mode="json")
                            payload["_meta"] = {
                                "user_id": user_id,
                                "request_id": body.request_id,
                                "index": index,
                                "image_ready": image_ready
                            }

                            row = Invoice(
                                image_url=url,
                                ocr_result_json=payload,
                                expense_item_id=None,
                                verified=False
                            )

                            db.add(row)
                            rows.append(row)

                        await db.commit()

                batch = pack_batch(body.request_id, rows)
                await cache_batch(user_id,batch)
                return batch

    async def detail(self, user_id: int, request_id: str) -> dict:
        """
        发票详情
        :param user_id:
        :param request_id:
        :return:
        """
        async with create_session() as db:
            rows = await load_rows(db, user_id, request_id)

            if not rows:
                raise InvoiceError("发票识别记录不存在", 404)

            return pack_batch(request_id, rows)

    async def confirm(self, user_id: int, body: InvoiceConfirmInput) -> dict:
        """
        发票确认
        :param user_id:
        :param body:
        :return:
        """
        corrections = validated_corrections(body.corrections)

        async with request_lock(body.request_id):
            async with create_session() as db:
                rows = await load_rows(
                    db,
                    user_id,
                    body.request_id,
                )

                row = next(
                    (
                        item
                        for item in rows
                        if item.ocr_result_json["_meta"]["index"]
                           == body.index
                    ),
                    None
                )

                if not row:
                    raise InvoiceError("发票不存在", 404)

                payload = dict(row.ocr_result_json)

                if not payload["_meta"].get("image_ready"):
                    raise InvoiceError("图片尚未读取成功， 请重新上传", 422)

                if row.verified:
                    if any(
                        payload.get(key) != value
                        for key, value in corrections.items()
                    ):
                        raise InvoiceError("发票已确认， 不能再次覆盖识别结果")
                else:
                    payload.update(corrections)

                    if payload.get("error") and not all(
                            payload.get(name) is not None
                            for name in (
                                    "invoice_type",
                                    "invoice_date",
                                    "total_amount",
                            )
                    ):
                        raise InvoiceError(
                            "请重新上传，或补全发票类型、"
                            "开票日期和价税合计",
                            422,
                        )

                    payload.update(
                        needs_confirm=False,
                        low_confidence_fields=[],
                        error=None,
                    )

                    row.ocr_result_json = payload
                    row.verified = True
                    row.verified_by = user_id
                    row.verified_at = utc_now()

                    await db.commit()

                batch = pack_batch(body.request_id, rows)

            await cache_batch(user_id, batch)
            return batch

    async def image(
            self,
            user_id: int,
            invoice_id: int,
    ):
        """
        图片权限服务
        :param user_id:
        :param invoice_id:
        :return:
        """
        async with create_session() as db:
            row = await db.get(Invoice, invoice_id)

            if row is None:
                raise InvoiceError("发票不存在", 404)

            owner = (
                (row.ocr_result_json or {})
                .get("_meta", {})
                .get("user_id")
            )

            if owner != user_id:
                approver = (
                    await db.scalar(
                        select(Expense.approver_id)
                        .join(
                            ExpenseItem,
                            ExpenseItem.expense_id == Expense.id,
                        )
                        .where(
                            ExpenseItem.id == row.expense_item_id
                        )
                    )
                    if row.expense_item_id
                    else None
                )

                if approver != user_id:
                    raise InvoiceError(
                        "无权查看这张发票",
                        403,
                    )

            if owner is None:
                raise InvoiceError(
                    "缺少发票上传人信息",
                    409,
                )

            image_url = row.image_url

        data = await load_image(image_url, owner)
        suffix = image_url.rsplit(".", 1)[-1]
        mime = {
            "jpg": "image/jpeg",
            "png": "image/png",
            "webp": "image/webp",
        }[suffix]

        return data, mime




