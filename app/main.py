# FastAPI入口

from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.config.settings import get_settings
from app.core.database import init_db, close_db
from app.core.milvus_client import init_milvus, close_milvus
from app.core.minio_client import init_minio, close_minio
from app.core.redis_client import init_redis, close_redis
from app.hermes.agent import init_hermes_agent, shutdown_hermes_agent
from app.api.v1 import feishu_gateway_webhook, knowledge
from app.services.conversation_engine.register_skill import get_skill_register
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
    """应用生命周期管理： 启动时初始化所有组件，关闭时释放资源"""
    settings = get_settings()

    # 按顺序初始化各个组件
    await init_db(settings)
    await init_redis(settings)
    await init_milvus(settings)
    await init_embedding(settings)
    await init_minio(settings)
    await init_hermes_agent(settings)
    await start_embedding_worker(settings)
    await start_cleanup_worker(settings)

    """
    yield 之前的代码在应用程序启动时运行（设置资源）。
    yield 之后的代码在应用程序关闭时运行（清理资源）。
    """
    yield

    # 按照逆序释放资源(逆序释放是保证程序稳定退出、避免 RuntimeError 的标准做法，也是微服务和资源管理中的最佳实践)
    await stop_cleanup_worker()
    await stop_embedding_worker()
    await shutdown_hermes_agent()
    await close_embedding()
    await close_minio()
    await close_milvus()
    await close_redis()
    await close_db()


settings = get_settings()
app = FastAPI(
    title=settings.app_name,
    lifespan=lifespan
)

app.include_router(feishu_gateway_webhook.router, prefix=f"{settings.app_prefix}", tags=["feishu_gateway_webhook"])
app.include_router(knowledge.router, prefix=f"{settings.app_prefix}", tags=["knowledge"])
app.include_router(knowledge.collection_router, prefix=f"{settings.app_prefix}", tags=["knowledge"])

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
