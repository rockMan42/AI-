import asyncio
import base64
import json
from io import BytesIO
import re
from urllib.parse import quote, uses_relative
from uuid import uuid4

from PIL import Image, ImageOps, UnidentifiedImageError

from app.config.settings import get_settings
from app.core.minio_client import read_bytes, upload_bytes
from app.core.redis_client import get_cache, set_cache
from app.services.conversation_engine.feishu import (
    FEISHU_API_BASE,
    _get_tenant_access_token,
    get_feishu_client,
)

"""
图片下载、保存和预处理
"""
MAX_IMAGE_BYTES = 10 * 1024 * 1024

IMAGE_FORMATS = {
    "JPEG": ("jpg", "image/jpeg"),
    "PNG": ("png", "image/png"),
    "WEBP": ("webp", "image/webp"),
}

def prepare_image(data: bytes) -> tuple[str, str, str]:
    """
    准备图片
    :param data:
    :return:
    """
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("图片为空或者超过10MB")

    try:
        with Image.open(BytesIO(data)) as image:
            if image.format not in IMAGE_FORMATS:
                raise ValueError("仅支持 JPEG, PNG 和 WEBP 格式的图片")

            if image.width * image.height > 40_000_000:
                raise ValueError("图片尺寸不能超过 40,000,000 像素")

            suffix, mime = IMAGE_FORMATS[image.format]

            image.load()
            normalized = ImageOps.exif_transpose(image).convert('RGB')
            normalized.thumbnail((2400, 2400))

            output = BytesIO()
            normalized.save(output, format="JPEG", quality=95)

    except (
        UnidentifiedImageError,
        OSError,
        Image.DecompressionBombError,
    ):
        raise ValueError("图片损坏或无法读取") from None

    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    return suffix, mime, f"data:image/jpeg;base64,{encoded}"

def object_key(image_url: str, user_id: int) -> str:
    bucket = get_settings().minio_bucket
    prefix = f"minio://{bucket}/invoices/{user_id}/"

    if not image_url.startswith(prefix):
        raise PermissionError("只能使用本人上传的发票图片")

    filename = image_url[len(prefix):]
    if not re.fullmatch(
        r"[a-f0-9]{32}\.(jpg|png|webp)",
        filename,
    ):
        raise ValueError("发票图片地址无效")

    return f"invoices/{user_id}/{filename}"

async def load_image(image_url: str, user_id: int) -> bytes:
    return await read_bytes(object_key(image_url, user_id), MAX_IMAGE_BYTES)


async def down_feishu_image(message_id: str, file_key: str) -> bytes:
    token = await _get_tenant_access_token()

    url = (
        f"{FEISHU_API_BASE}/im/v1/messages/"
        f"{quote(message_id, safe='')}/resources/"
        f"{quote(file_key, safe='')}"
    )

    async with asyncio.timeout(30):
        async with get_feishu_client().stream(
                "GET",
                url,
                params={"type": "image"},
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
        ) as response:
            response.raise_for_status()

            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                chunks.extend(chunk)
                if len(chunks) > MAX_IMAGE_BYTES:
                    raise ValueError("图片不能超过 10MB")

    return bytes(chunks)


async def store_feishu_image(
        user_id: int,
        message_id: str,
        file_key: str,
) -> str:
    cache_key = f"dep:tmp:feishu_url:{file_key}"
    cached = await get_cache(cache_key)

    if cached:
        saved = json.loads(cached)
        if (
            saved["user_id"] == user_id
            and saved["message_id"] == message_id
        ):
            return saved["image_url"]

    data = await down_feishu_image(message_id, file_key)
    suffix, mime, _ = await asyncio.to_thread(
        prepare_image,
        data,
    )

    key = f"invoices/{user_id}/{uuid4().hex}.{suffix}"
    await upload_bytes(data,key,mime)

    image_url = f"minio://{get_settings().minio_bucket}/{key}"

    await set_cache(
        cache_key,
        json.dumps({
            "user_id": user_id,
            "message_id": message_id,
            "image_url": image_url
        }),
        expire=7200
    )

    return image_url


