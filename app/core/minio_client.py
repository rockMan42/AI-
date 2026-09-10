import asyncio
from pathlib import Path

from minio import Minio, S3Error
from minio.commonconfig import ENABLED
from minio.sse import SseS3
from minio.versioningconfig import VersioningConfig

from app.config.settings import Settings

_client: Minio | None = None
_bucket_name: str | None = None
_sse_enabled = False

async def init_minio(settings: Settings) -> None:
    global _client, _bucket_name, _sse_enabled

    if "://" in settings.minio_endpoint:
        raise RuntimeError("MINIO_ENDPOINT 不能包含 URL scheme")

    access_key = settings.minio_access_key.get_secret_value()
    secret_key = settings.minio_secret_key.get_secret_value()
    if not access_key or not secret_key:
        raise RuntimeError("MinIO 凭证未配置")

    client = Minio(
        endpoint=settings.minio_endpoint,
        access_key=access_key,
        secret_key=secret_key,
        secure=settings.minio_secure,
        region=settings.minio_region,
    )

    exists = await asyncio.to_thread(
        client.bucket_exists,
        settings.minio_bucket,
    )
    if not exists:
        if not settings.minio_auto_create_bucket:
            raise RuntimeError(
                f"MinIO 桶不存在: {settings.minio_bucket}"
            )
        await asyncio.to_thread(
            client.make_bucket,
            settings.minio_bucket,
            settings.minio_region,
        )

    # 版本控制可以减少管理员误删带来的不可恢复风险
    await asyncio.to_thread(
        client.set_bucket_versioning,
        settings.minio_bucket,
        VersioningConfig(ENABLED),
    )

    _client = client
    _bucket_name = settings.minio_bucket
    _sse_enabled = settings.minio_sse_enabled


async def close_minio() -> None:
    global _client, _bucket_name
    _client = None
    _bucket_name = None


def _get_client() -> tuple[Minio, str]:
    if _client is None or _bucket_name is None:
        raise RuntimeError("MinIO 尚未初始化")
    return _client, _bucket_name


async def upload_file(
    local_path: Path,
    object_key: str,
    content_type: str,
    sha256: str,
) -> None:
    client, bucket = _get_client()

    kwargs = {
        "bucket_name": bucket,
        "object_name": object_key,
        "file_path": str(local_path),
        "content_type": content_type,
        "metadata": {"sha256": sha256},
    }
    if _sse_enabled:
        kwargs["sse"] = SseS3()

    await asyncio.to_thread(client.fput_object, **kwargs)


async def delete_file(object_key: str) -> None:
    client, bucket = _get_client()
    await asyncio.to_thread(client.remove_object, bucket, object_key)


async def check_minio() -> str:
    try:
        client, bucket = _get_client()
        exists = await asyncio.to_thread(client.bucket_exists, bucket)
        return "connected" if exists else "bucket_missing"
    except (RuntimeError, S3Error):
        return "error"
