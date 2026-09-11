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

from app.core.embedding_client import (
    close_embedding,
    init_embedding,
)
from app.services.knowledge.knowledge_index_service import (
    start_embedding_worker,
    stop_embedding_worker,
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()

    async with AsyncExitStack() as stack:
        await init_db(settings)
        stack.push_async_callback(close_db)

        await init_redis(settings)
        stack.push_async_callback(close_redis)

        await init_milvus(settings)
        stack.push_async_callback(close_milvus)

        await init_embedding(settings)
        stack.push_async_callback(close_embedding)

        await init_minio(settings)
        stack.push_async_callback(close_minio)

        service = RAGService(settings)
        stack.push_async_callback(service.close)
        app.state.rag_service = service

        # 注册前就安排清理，覆盖注册中途失败的情况
        stack.push_async_callback(shutdown_hermes_agent)
        await init_hermes_agent(settings)

        await start_embedding_worker(settings)
        stack.push_async_callback(stop_embedding_worker)

        await start_cleanup_worker(settings)
        stack.push_async_callback(stop_cleanup_worker)

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
