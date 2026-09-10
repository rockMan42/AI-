# API路由层
from functools import lru_cache

from pydantic import Field, SecretStr
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
    milvus_token: SecretStr = SecretStr("")
    milvus_database: str = "default"
    milvus_collection: str = "enterprise_knowledge"
    milvus_secure: bool = False
    milvus_server_pem_path: str | None = None
    milvus_server_name: str | None = None
    milvus_timeout_seconds: float = Field(default=15.0, gt=0)
    milvus_embedding_dimension: int = 1024
    milvus_metric_type: str = "COSINE"
    milvus_hnsw_m: int = 16
    milvus_hnsw_ef_construction: int = 200
    milvus_search_ef: int = 128
    milvus_insert_batch_size: int = Field(default_factory=500,ge=1,le=5000)

    # 百炼 Embedding & 百练平台 API KEY
    dashscope_api_key: str  = ""
    embedding_base_url: str = (
        "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )
    embedding_model: str = "text-embedding-v3"
    embedding_dimension: int = 1024
    embedding_batch_size: int = Field(default=10, ge=1, le=10)
    embedding_max_tokens: int = 8192
    embedding_timeout_seconds: float = Field(default=30.0, gt=0)
    embedding_max_retries: int = Field(default=3, ge=0, le=10)
    embedding_backoff_seconds: float = Field(default=1.0, gt=0)

    # 向量化 Worker
    knowledge_embedding_worker_enabled: bool = True
    knowledge_embedding_poll_seconds: float = Field(default=5.0, gt=0)
    knowledge_embedding_worker_batch_size: int = Field(
        default=5,
        ge=1,
        le=50,
    )
    knowledge_embedding_max_attempts: int = Field(
        default=3,
        ge=1,
        le=10,
    )
    knowledge_embedding_stale_seconds: int = Field(
        default=300,
        ge=30,
    )

    # 飞书用户白名单
    feishu_allowed_users: str = ""

    # 飞书应用凭证
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_verification_token: str = ""

    # encrypt
    encrypt_key: str = ""

    # REST API JWT
    auth_jwt_secret: SecretStr = SecretStr("")
    auth_jwt_issuer: str = "ai-digital-employee"
    auth_jwt_audience: str= "knowledge-api"

    # MinIO 配置
    minio_endpoint: str = "127.0.0.1:9000"
    minio_access_key: SecretStr = SecretStr("")
    minio_secret_key: SecretStr = SecretStr("")
    minio_secure: bool = False
    minio_bucket: str = "knowledge-documents"
    minio_region: str = "us-east-1"
    minio_auto_create_bucket: bool = False
    minio_sse_enabled: bool = False

    # ClamAV 配置
    clamav_host: str = "127.0.0.1"
    clamav_port: int = 3310
    clamav_timeout_seconds: float = 15.0
    virus_scan_required: bool = True

    # 文档安全限制
    knowledge_max_upload_bytes: int = 50 * 1024 *1024
    knowledge_max_docx_uncompressed_bytes: int = 100 * 1024 * 1024
    knowledge_max_docx_compression_ratio: int = 100
    knowledge_max_docx_entries: int = Field(default=10_000, ge=1)
    knowledge_max_pdf_pages: int = Field(default=500, ge=1)
    knowledge_temp_dir: str | None= None
    knowledge_token_encoding: str = "cl100k_base"
    knowledge_ocr_enabled: bool = True
    knowledge_ocr_language: str = "chi_sim+eng"
    knowledge_ocr_dpi: int = Field(default=200, ge=72, le=600)
    knowledge_ocr_min_chars: int = Field(default=20, ge=0)
    knowledge_cleanup_poll_seconds: float = Field(default=30.0, gt=0)
    knowledge_cleanup_batch_size: int = Field(default=10, ge=1, le=100)

    # 指定了 env_file = ".env"，就不再需要手动 load_dotenv() 了, 启动时自动读取.env配置
    model_config = {
        "env_file":".env",
        "env_file_encoding":"utf-8"
    }

    @property
    def milvus_uri(self) -> str:
        scheme = "https" if self.milvus_secure else "http"
        return f"{scheme}://{self.milvus_host}:{self.milvus_port}"


def get_settings() -> Settings:
    return Settings()

