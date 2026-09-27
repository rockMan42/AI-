import asyncio
import logging

from fastapi import HTTPException
from pydantic import ValidationError

from app.api.v1.feishu_leave import callback_actor
from app.services.expense.expense_chat import expense_card, reopened_card
from app.services.expense.approval_cards import query_card
from app.services.expense.approval_service import approval_detail
from app.services.expense.expense_service import ExpenseService
from app.services.expense.rules import ExpenseError
from app.services.business_rules.common import RuleError
from app.schemas.expense import ExpensePrepareInput, ExpenseSupplement
from app.schemas.invoice import InvoiceConfirmInput
from app.services.expense.invoice_service import InvoiceService, InvoiceError
from app.services.expense.auto_flow import (
    build_next_expense_card, infer_expense_type, supplement_form_card,
)
from app.services.card_form import parse_form_values

log = logging.getLogger(__name__)
service = ExpenseService()


async def handle_expense_card_data(data):
    event = data.get("event") or {}
    value = (event.get("action") or {}).get("value") or {}
    open_id = (event.get("operator") or {}).get("open_id", "")

    operation = str(
        value.get("operation") or value.get("action") or ""
    ).strip()

    if operation == "status":
        expense_id = value.get("expense_id")
        if type(expense_id) is not int or expense_id <= 0:
            return {"toast": {"type": "error", "content":
                    "进度卡片已失效，请重新查询"}}
        try:
            async with asyncio.timeout(5):
                user = await callback_actor(open_id)
                detail = await approval_detail(user.user_id, expense_id)
            return {"toast": {"type": "success", "content": "审批进度已更新"},
                    "card": {"type": "raw", "data": query_card(detail)}}
        except ExpenseError as exc:
            if exc.status_code == 403:
                raise HTTPException(403, str(exc)) from None
            return {"toast": {"type": "error", "content": str(exc)}}
        except TimeoutError:
            return {"toast": {"type": "warning", "content":
                    "审批进度查询超时，请稍后重试"}}
        except HTTPException:
            raise
        except Exception:
            log.exception("expense_status_card_failed")
            return {"toast": {"type": "error", "content":
                    "审批进度暂时无法查看，请稍后重试"}}

    if operation in {"supplement_open", "supplement_save"}:
        try:
            async with asyncio.timeout(10):
                user = await callback_actor(open_id)
                reference = InvoiceConfirmInput(
                    request_id=value.get("request_id"),
                    index=value.get("index"),
                )
                invoice_service = InvoiceService(None)
                batch = await invoice_service.detail(user.user_id, reference.request_id)
                item = next((row for row in batch["results"] if row["index"] == reference.index), None)
                if item is None or not item["verified"]:
                    raise ExpenseError("请先确认这张发票的识别结果", 409)
                if operation == "supplement_open":
                    next_card = supplement_form_card(reference.request_id, item)
                    message = "请在卡片中补充费用信息"
                else:
                    expected = {"expense_type", "city", "check_in", "check_out", "seat"}
                    try:
                        fields = parse_form_values(
                            (event.get("action") or {}).get("form_value") or {}, expected,
                        )
                    except ValueError:
                        raise ExpenseError("编辑卡片已失效，请重新打开", 409) from None
                    current = item.get("_expense") or {}
                    source = {"city": "expense_city", "check_in": "service_start_date",
                              "check_out": "service_end_date", "seat": "transport_seat"}
                    merged = {
                        name: fields[name].strip() or None
                        if name in fields else current.get(name) or item.get(source.get(name))
                        for name in expected
                    }
                    merged["expense_type"] = (
                        fields.get("expense_type") or current.get("expense_type")
                        or infer_expense_type(item)
                    )
                    body = ExpenseSupplement.model_validate(merged)
                    await service.supplement(user.user_id, item["invoice_id"], body)
                    batch = await invoice_service.detail(user.user_id, reference.request_id)
                    next_card = await build_next_expense_card(user.user_id, batch)
                    if next_card is None:
                        raise ExpenseError("发票信息暂不可用，请稍后重试", 503)
                    message = "费用信息已保存"
            return {"toast": {"type": "success", "content": message},
                    "card": {"type": "raw", "data": next_card}}
        except (ExpenseError, InvoiceError) as exc:
            if exc.status_code == 403:
                raise HTTPException(403, str(exc)) from None
            return {"toast": {"type": "error", "content": str(exc)}}
        except ValidationError:
            return {"toast": {"type": "error", "content":
                    "请检查费用类型、城市、日期或座席信息"}}
        except TimeoutError:
            return {"toast": {"type": "warning", "content":
                    "处理结果暂未确认，请稍后重新打开卡片"}}
        except HTTPException:
            raise
        except Exception:
            log.exception("expense_supplement_card_failed")
            return {"toast": {"type": "error", "content":
                    "费用信息暂时无法保存，请稍后重试"}}

    if operation in {"prepare", "select_trip", "prepare_override"}:
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
                    override_reason = ""
                    if operation == "prepare_override":
                        try:
                            fields = parse_form_values(
                                (event.get("action") or {}).get("form_value") or {},
                                {"reason"},
                            )
                        except ValueError:
                            raise ExpenseError("请重新打开超标申请卡片", 409) from None
                        override_reason = fields.get("reason", "")
                    body = ExpensePrepareInput(
                        trip_id=trip_id,
                        invoice_ids=invoice_ids,
                        accept_overrides=operation == "prepare_override",
                        override_reason=override_reason,
                    )
                except ValidationError:
                    raise ExpenseError("请填写合理的超标原因后重试", 422) from None

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
        except RuleError as exc:
            if exc.code == "APPROVER_INVALID":
                return {"toast": {"type": "error", "content":
                        "审批人员配置无效，请管理员检查主管、财务和出纳是否为不同的在职人员；发票无需重新上传。"}}
            return {"toast": {"type": "error", "content":
                    "报销规则暂不可用，请联系管理员检查配置；发票无需重新上传。"}}
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
        or operation not in {"confirm", "cancel", "resubmit"}
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
            if operation == "resubmit":
                result = await service.reopen_rejected(
                    user.user_id, expense_id,
                )
                return {
                    "toast": {
                        "type": "success",
                        "content": "已开放修改，请修改后重新确认提交",
                    },
                    "card": {
                        "type": "raw",
                        "data": await reopened_card(user.user_id, result),
                    },
                }
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
