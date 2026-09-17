import asyncio
import logging

from fastapi import HTTPException
from pydantic import ValidationError

from app.api.v1.feishu_leave import callback_actor
from app.services.expense.expense_chat import expense_card
from app.services.expense.expense_service import ExpenseService
from app.services.expense.rules import ExpenseError
from app.schemas.expense import ExpensePrepareInput


log = logging.getLogger(__name__)
service = ExpenseService()


async def handle_expense_card_data(data):
    event = data.get("event") or {}
    value = (event.get("action") or {}).get("value") or {}
    open_id = (event.get("operator") or {}).get("open_id", "")

    operation = str(
        value.get("operation") or value.get("action") or ""
    ).strip()

    if operation in {"prepare", "select_trip"}:
        try:
            async with asyncio.timeout(10):
                user = await callback_actor(open_id)
                raw_invoice_ids = value.get("invoice_ids")
                trip_id = value.get("trip_id")

                if (
                    not isinstance(raw_invoice_ids, list)
                    or not raw_invoice_ids
                ):
                    raise ExpenseError("发票参数无效", 422)

                try:
                    invoice_ids = [
                        int(item) for item in raw_invoice_ids
                    ]
                except (TypeError, ValueError):
                    raise ExpenseError("发票参数无效", 422) from None

                if any(item <= 0 for item in invoice_ids):
                    raise ExpenseError("发票参数无效", 422)

                if isinstance(trip_id, str) and trip_id.isdigit():
                    trip_id = int(trip_id)
                if (
                    trip_id is not None
                    and (type(trip_id) is not int or trip_id <= 0)
                ):
                    raise ExpenseError("出差申请参数无效", 422)

                try:
                    body = ExpensePrepareInput(
                        trip_id=trip_id,
                        invoice_ids=invoice_ids,
                    )
                except ValidationError as exc:
                    raise ExpenseError(
                        f"报销卡片参数有误：{exc.errors()[0]['msg']}", 422,
                    ) from None

                result = await service.prepare(
                    user.user_id,
                    body,
                )
            return {
                "toast": {
                    "type": "success",
                    "content": "报销校验已完成",
                },
                "card": {
                    "type": "raw",
                    "data": expense_card(result),
                },
            }
        except ExpenseError as exc:
            if exc.status_code == 403:
                raise HTTPException(403, str(exc)) from None
            return {"toast": {"type": "error", "content": str(exc)}}
        except ValidationError as exc:
            fields = [
                ".".join(map(str, error["loc"]))
                for error in exc.errors()
            ]
            log.error("expense_internal_validation_failed fields=%s", fields)
            return {
                "toast": {
                    "type": "error",
                    "content": "报销业务数据校验失败，请管理员检查服务日志",
                }
            }
        except TimeoutError:
            return {
                "toast": {
                    "type": "warning",
                    "content": "校验尚未完成，请再次点击。",
                }
            }
        except HTTPException:
            raise
        except Exception:
            log.exception("expense_prepare_card_failed")
            return {
                "toast": {
                    "type": "error",
                    "content": "报销校验暂不可用，请稍后重试。",
                }
            }

    expense_id = value.get("expense_id")

    if isinstance(expense_id, str) and expense_id.isdigit():
        expense_id = int(expense_id)

    if (
        type(expense_id) is not int
        or expense_id <= 0
        or operation not in {"confirm", "cancel"}
    ):
        log.warning(
            "expense_card_invalid operation=%s keys=%s",
            operation,
            sorted(value),
        )
        raise HTTPException(
            422,
            "报销卡片已失效，请重新完成发票确认后使用新卡片",
        )

    try:
        async with asyncio.timeout(2):
            user = await callback_actor(open_id)
            if operation == "confirm":
                result = await service.submit(user.user_id, expense_id)
            else:
                result = await service.cancel(user.user_id, expense_id)

        return {
            "toast": {
                "type": "success",
                "content": (
                    "已记录提交，正在发送财务"
                    if result["status"] == "submitting"
                    else "操作已完成"
                ),
            },
            "card": {
                "type": "raw",
                "data": expense_card(result),
            },
        }

    except ExpenseError as exc:
        if exc.status_code == 403:
            raise HTTPException(403, str(exc)) from None
        return {"toast": {"type": "error", "content": str(exc)}}
    except TimeoutError:
        return {
            "toast": {
                "type": "warning",
                "content": "处理结果暂未确认，请查询报销单或再次点击。",
            }
        }
    except HTTPException:
        raise
    except Exception:
        log.exception("expense_card_failed")
        return {
            "toast": {
                "type": "error",
                "content": "报销服务暂不可用，请稍后重试。",
            }
        }
