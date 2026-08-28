from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from app.config.settings import Settings


async def init_db(settings: Settings):
    """初始化 MySql 异步引擎和会话工厂"""
    global  _engine, _session_factory

    _engine = create_async_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True
    )

    _session_factory = async_sessionmaker(
        _engine,
        class_=AsyncSession,
        expire_on_commit=False
    )


async def get_session() -> AsyncSession:
    """获取数据库会话（用于依赖注入）"""
    async with _session_factory() as session:
        yield session

async def close_db():
    """关闭数据库引擎"""
    if _engine:
        await _engine.dispose()

async def check_db() -> str:
    """检查数据库连接状态"""
    try:
        async with _engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
            return "connected"
    except Exception as e:
        return f"error:{e}"

