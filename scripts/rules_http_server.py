"""仅供本机隔离验收：原应用路由/鉴权，限制生命周期中的外部副作用。"""

import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import Depends
from fastapi.responses import JSONResponse
from tools.mcp_tool import register_mcp_servers, shutdown_mcp_servers

from app.config.settings import get_settings
from app.core.database import close_db, init_db
from app.core.redis_client import close_redis, init_redis
from app.hermes.agent import FINANCE_SERVER_NAME, _mcp_config
from app.main import app
from app.security.auth import get_current_user
from app.services.business_rules.worker import start_rule_workers, stop_rule_workers
from app.services.expense.approval_service import poll_all
from app.services.expense.delivery import ExpenseDelivery


@asynccontextmanager
async def acceptance_lifespan(app):
    settings = get_settings()
    if not settings.database_url.rsplit("/", 1)[-1].startswith("rules_http_"):
        raise RuntimeError("HTTP 验收服务只能连接 rules_http_ 专用库")
    await init_db(settings)
    await init_redis(settings)
    await start_rule_workers()
    config = _mcp_config(settings)
    await asyncio.to_thread(
        register_mcp_servers, {FINANCE_SERVER_NAME: config[FINANCE_SERVER_NAME]}
    )
    delivery = ExpenseDelivery()
    await delivery.start()

    async def poll():
        while True:
            await poll_all()
            await asyncio.sleep(1)

    polling = asyncio.create_task(poll())
    try:
        yield
    finally:
        polling.cancel()
        with suppress(asyncio.CancelledError):
            await polling
        await delivery.close()
        await stop_rule_workers()
        await asyncio.to_thread(shutdown_mcp_servers)
        await close_redis()
        await close_db()


app.router.lifespan_context = acceptance_lifespan


@app.get("/api/v1/_acceptance/redis")
async def redis_diagnostic(user=Depends(get_current_user)):
    """仅验收入口：显示实际连接地址及连通性，帮助区分回源与重连。"""
    from app.core import redis_client

    client = redis_client.redis_client
    kwargs = client.connection_pool.connection_kwargs
    result = {k: kwargs.get(k) for k in ("host", "port", "db")}
    result["available_connections"] = len(client.connection_pool._available_connections)
    result["in_use_connections"] = len(client.connection_pool._in_use_connections)
    try:
        result["ping"] = await asyncio.wait_for(client.ping(), timeout=2)
        return result
    except Exception as exc:
        result["error"] = str(exc)[:300]
        return JSONResponse(result, status_code=503)
