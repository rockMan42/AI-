
import redis.asyncio as aioredis

from app.config.settings import Settings

# 定义全局变量，用于存储 Redis 客户端实例
redis_client: aioredis.Redis = None

async def init_redis(settings: Settings):
    """创建异步客户端实例"""
    # 定义全局变量
    global redis_client

    # 防重复创建
    if redis_client is not None:
        return redis_client

    redis_client = aioredis.from_url(
        settings.redis_url,
        decode_responses=True
    )

    try:
        # 启动式ping验证连接可用性
        if await redis_client.ping():
            print("redis连接成功")
    except Exception as e:
        print(f"redis连接失败:{e}")
        raise e

    return redis_client




async def close_redis():
    """安全关闭客户端连接"""
    try:
        if redis_client is not None:
            await redis_client.close()
    except Exception as e:
        print("redis关闭失败")
        raise e

async def check_redis():
    global redis_client
    try:
        if redis_client is None:
            return "error: redis client not initialized"
        await redis_client.ping()
        return "connected"
    except Exception as e:
        print(f"error:{e}")
        raise e
