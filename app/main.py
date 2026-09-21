# FastAPI入口
from contextlib import AsyncExitStack, asynccontextmanager
from fastapi import FastAPI
from app.config.settings import get_settings
from app.core.database import init_db, close_db
from app.core.milvus_client import init_milvus, close_milvus
from app.core.minio_client import init_minio, close_minio
from app.core.redis_client import init_redis, close_redis
from app.hermes.agent import init_hermes_agent, shutdown_hermes_agent
from app.api.v1 import feishu_gateway_webhook, knowledge
from app.services.conversation_engine.register_skill import get_skill_register
from app.services.knowledge.rag_service import RAGService
from app.services.knowledge.document_cleanup import (
    start_cleanup_worker,
    stop_cleanup_worker,
)
from uuid import uuid4

from fastapi.responses import JSONResponse

from app.api.v1 import auth, permission_admin
from app.schemas.permission import AccessDenied, PermissionUnavailable
from app.services.permission_audit import audit_ip, audit_request_id
from app.services.permission_worker import (
    start_permission_worker,
    stop_permission_worker,
)
from app.api.v1 import lead
from app.api.v1 import base_call_back
from app.api.v1 import expense
from app.services.expense.delivery import ExpenseDelivery
from app.api.v1 import invoice
from app.services.expense.chat import InvoiceChat
from app.services.expense.invoice_ocr import InvoiceOCRService
from app.services.expense.invoice_service import InvoiceService
from app.api.v1 import requisition
from app.api.v1 import holiday
from app.services.holiday_cron import (
    start_holiday_worker,
    stop_holiday_worker,
)
from app.api.v1 import leave, feishu_leave
from app.services.attendance.leave_notifications import (
    start_leave_notification_worker,
    stop_leave_notification_worker,
)
from app.api.v1 import attendance
from app.core.embedding_client import (
    close_embedding,
    init_embedding,
)
from app.services.knowledge.knowledge_index_service import (
    start_embedding_worker,
    stop_embedding_worker,
)

from app.services.conversation_engine.feishu import init_feishu_client, close_feishu_client

@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()

    async with AsyncExitStack() as stack:
        await init_feishu_client()
        stack.push_async_callback(close_feishu_client)
        await feishu_gateway_webhook.init_model_runner()
        await init_db(settings)
        stack.push_async_callback(close_db)

        await init_redis(settings)
        stack.push_async_callback(close_redis)

        await start_permission_worker()
        stack.push_async_callback(stop_permission_worker)

        await init_milvus(settings)
        stack.push_async_callback(close_milvus)

        await init_embedding(settings)
        stack.push_async_callback(close_embedding)

        await init_minio(settings)
        stack.push_async_callback(close_minio)

        invoice_ocr = InvoiceOCRService(
            api_key=settings.dashscope_api_key,
            api_url=settings.invoice_ocr_api_url,
            model=settings.invoice_ocr_model,
            confidence_threshold=(
                settings.invoice_ocr_confidence_threshold
            ),
        )
        stack.push_async_callback(invoice_ocr.close)

        invoice_service = InvoiceService(invoice_ocr)
        invoice_chat = InvoiceChat(invoice_service)

        stack.push_async_callback(invoice_chat.close)

        app.state.invoice_ocr_service = invoice_ocr
        app.state.invoice_service = invoice_service
        app.state.invoice_chat = invoice_chat

        service = RAGService(settings)
        stack.push_async_callback(service.close)
        app.state.rag_service = service


        # 注册前就安排清理，覆盖注册中途失败的情况
        stack.push_async_callback(shutdown_hermes_agent)
        await init_hermes_agent(settings)
        stack.push_async_callback(feishu_gateway_webhook.close_model_runner)

        await start_embedding_worker(settings)
        stack.push_async_callback(stop_embedding_worker)

        await start_cleanup_worker(settings)
        stack.push_async_callback(stop_cleanup_worker)

        await start_leave_notification_worker()
        stack.push_async_callback(stop_leave_notification_worker)

        await start_holiday_worker()
        stack.push_async_callback(stop_holiday_worker)

        expense_delivery = ExpenseDelivery()
        await expense_delivery.start()
        stack.push_async_callback(expense_delivery.close)

        yield

settings = get_settings()
app = FastAPI(
    title=settings.app_name,
    lifespan=lifespan
)

app.include_router(feishu_gateway_webhook.router, prefix=f"{settings.app_prefix}", tags=["feishu_gateway_webhook"])
app.include_router(knowledge.router, prefix=f"{settings.app_prefix}", tags=["knowledge"])
app.include_router(knowledge.collection_router, prefix=f"{settings.app_prefix}", tags=["knowledge"])
app.include_router(knowledge.search_router, prefix=settings.app_prefix, tags=["knowledge"],)
app.include_router(attendance.router, prefix=settings.app_prefix, tags=["attendance"],)
app.include_router(leave.router,prefix=settings.app_prefix,tags=["leave"],)
app.include_router(feishu_leave.router, prefix=settings.app_prefix, tags=["feishu_leave"],)
app.include_router( holiday.router, prefix=settings.app_prefix, tags=["holiday"],)
app.include_router(requisition.router, prefix=settings.app_prefix, tags=["requisition"],)
app.include_router(invoice.router, prefix=settings.app_prefix, tags=["invoice"],)
app.include_router(expense.router,prefix=settings.app_prefix,tags=["expense"],)
app.include_router(base_call_back.router, prefix=settings.app_prefix, tags=["feishu_callback"],)
app.include_router(lead.router, prefix=settings.app_prefix, tags=["lead"],)
app.include_router(auth.router, prefix=settings.app_prefix, tags=["auth"])
app.include_router(permission_admin.router, prefix=settings.app_prefix, tags=["permission"],)

# 应用启动时注册skill
register = get_skill_register()
register.load_from_directory("./app/hermes/skills")

@app.get(f"{settings.app_prefix}/health")
async def check_health():
    """健康检查"""
    from app.core.database import check_db
    from app.core.redis_client import check_redis
    from app.core.milvus_client import check_milvus
    return {
        "status":"ok",
        "db":await check_db(),
        "redis":await check_redis(),
        "milvus":await check_milvus()
    }


@app.middleware("http")
async def attendance_timing(request, call_next):
    if not request.url.path.startswith(f"{settings.app_prefix}/attendance/"):
        return await call_next(request)
    from app.core.rag_context import traced, phase
    @traced
    async def invoke():
        with phase("attendance_http") as metrics:
            result = await call_next(request)
            metrics["status_code"] = result.status_code
            return result
    return await invoke()


@app.exception_handler(AccessDenied)
async def access_denied_handler(request, exc):
    return JSONResponse(
        {"detail": str(exc)},
        status_code=403,
    )


@app.exception_handler(PermissionUnavailable)
async def permission_unavailable_handler(request, exc):
    return JSONResponse(
        {"detail": str(exc)},
        status_code=503,
    )


@app.middleware("http")
async def permission_audit_context(request, call_next):
    request_token = audit_request_id.set(uuid4().hex)
    ip_token = audit_ip.set(
        request.client.host if request.client else None
    )

    try:
        return await call_next(request)
    finally:
        audit_request_id.reset(request_token)
        audit_ip.reset(ip_token)