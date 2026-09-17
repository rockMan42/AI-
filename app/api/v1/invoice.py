import asyncio
import logging

from pydantic import ValidationError

from app.api.v1.feishu_leave import callback_actor
from app.models import User
from app.schemas.invoice import InvoiceOCRInput, InvoiceConfirmInput
from app.security.auth import get_current_user
from app.services.expense.cards import invoice_card
from app.services.expense.chat import finish_flow, refresh_summary
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse
from app.services.expense.invoice_service import InvoiceError
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    Response,
)
router = APIRouter(prefix="/expense/invoice")
log = logging.getLogger(__name__)


async def invoke(operation):
    try:
        return await operation

    except InvoiceError as exc:
        raise HTTPException(
            exc.status_code,
            str(exc),
        ) from None

    except PermissionError:
        raise HTTPException(
            403,
            "无权访问这张发票",
        ) from None

    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None

    except TimeoutError:
        raise HTTPException(
            504,
            "处理超时，请重试",
        ) from None

    except Exception as exc:
        log.error(
            "invoice_api_failed error_type=%s",
            type(exc).__name__,
        )
        raise HTTPException(
            503,
            "发票服务暂不可用，请重试",
        ) from None


@router.post("/ocr")
async def recognize(
        body: InvoiceOCRInput,
        request: Request,
        user: User = Depends(get_current_user)


):
    batch = await invoke(request.app.state.invoice_service.recognize(user.user_id, body))

    return {
        "code": 200,
        "data": batch
    }

@router.post("/confirm")
async def confirm(
        body: InvoiceConfirmInput,
        request: Request,
        user: User = Depends(get_current_user)
):
    batch = await invoke(request.app.state.invoice_service.confirm(user.user_id, body))

    await finish_flow(user.user_id, batch)

    return {
        "code": 200,
        "data": batch
    }

@router.get("/{invoice_id}/image")
async def image(
        invoice_id: int,
        request: Request,
        user: User = Depends(get_current_user)
):
    data, mime = await invoke(request.app.state.invoice_service.image(user.user_id, invoice_id))

    return Response(
        content=data,
        media_type=mime,
        headers={
            "Cache-Control": "private, no-store",
        },
    )




async def handle_invoice_card_data(data: dict, service):
    event = data.get("event") or {}
    value = (
        (event.get("action") or {}).get("value")
        or {}
    )
    open_id = (
        (event.get("operator") or {}).get("open_id", "")
    )

    try:
        async with asyncio.timeout(2):
            actor = await callback_actor(open_id)

            body = InvoiceConfirmInput(
                request_id=value.get("request_id"),
                index=value.get("index"),
            )
            operation = value.get("operation")

            if operation == "modify":
                batch = await service.detail(
                    actor.user_id,
                    body.request_id,
                )
            elif operation == "confirm":
                batch = await service.confirm(
                    actor.user_id,
                    body,
                )
            else:
                raise InvoiceError(
                    "不支持的发票操作",
                    422,
                )

            item = next(
                (
                    item
                    for item in batch["results"]
                    if item["index"] == body.index
                ),
                None,
            )

            if item is None:
                raise InvoiceError("发票不存在", 404)

            if operation == "modify" and item["verified"]:
                raise InvoiceError("发票已经确认")

        if operation == "confirm":
            await finish_flow(actor.user_id, batch)

        result = {
            "toast": {
                "type": "success",
                "content": (
                    "已确认"
                    if operation == "confirm"
                    else "请按卡片提示修改"
                ),
            },
            "card": {
                "type": "raw",
                "data": invoice_card(
                    body.request_id,
                    item,
                    editing=operation == "modify",
                ),
            },
        }
        if operation == "confirm":
            return JSONResponse(
                result,
                background=BackgroundTask(
                    refresh_summary, actor.user_id, body.request_id, service,
                ),
            )
        return result

    except InvoiceError as exc:
        if exc.status_code == 403:
            raise HTTPException(
                403,
                str(exc),
            ) from None

        return {
            "toast": {
                "type": "error",
                "content": str(exc),
            }
        }

    except ValidationError:
        return {
            "toast": {
                "type": "error",
                "content": "卡片参数无效",
            }
        }

    except TimeoutError:
        return {
            "toast": {
                "type": "warning",
                "content": "处理结果暂未确认，请再次点击原按钮。",
            }
        }

    except HTTPException:
        raise

    except Exception as exc:
        log.error(
            "invoice_callback_failed error_type=%s",
            type(exc).__name__,
        )
        return {
            "toast": {
                "type": "error",
                "content": "服务暂不可用，请重试",
            }
        }
