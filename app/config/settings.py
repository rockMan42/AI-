# API路由层
from functools import lru_cache
from pydantic_settings import BaseSettings

# 继承了BaseSettings，因此允许从环境变量中自动读取配置映射到对应的字段,不区分大小写
class Settings(BaseSettings):
    # FastAPI 基础配置
    app_name: str = "AI数字员工平台"
    app_prefix: str = "/api/v1"
    debug: bool = False

    # Mysql 连接配置
    database_url: str = ""
    db_pool_size: int = 10
    db_max_overflow: int = 20

    # Redis 连接配置
    redis_url: str = ""

    # Milvus 连接配置
    milvus_host: str = "localhost"
    milvus_port: int = 19530

    # 百练平台 API KEY
    dashscope_api_key: str  = ""

    # 飞书用户白名单
    feishu_allowed_users: str = ""

    # 飞书应用凭证
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_verification_token: str = ""
    # encrypt
    encrypt_key: str = ""
    # 指定了 env_file = ".env"，就不再需要手动 load_dotenv() 了, 启动时自动读取.env配置
    model_config = {
        "env_file":".env",
        "env_file_encoding":"utf-8"
    }


def get_settings() -> Settings:
    return Settings()

