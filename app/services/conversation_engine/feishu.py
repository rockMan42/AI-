import json

import httpx
from app.config.settings import get_settings
from app.core.redis_client import get_cache, set_cache

# 飞书 API 基础地址
FEISHU_API_BASE = "https://open.feishu.cn/open-apis"
access_token_key = "dep:tenant_access_token"

async def _get_tenant_access_token() -> str:
    """获取飞书用户访问令牌（自动缓存和续期）"""

    # 从缓存中获取令牌
    token = await get_cache(access_token_key)
    if token is not None:
        return token

    settings = get_settings()
    async with httpx.AsyncClient() as client:
        res = await client.post(
            f"{FEISHU_API_BASE}/auth/v3/tenant_access_token/internal",
            json={
                "app_id": settings.feishu_app_id,
                "app_secret": settings.feishu_app_secret,
            },
        )

        data =  res.json()

        token = data["tenant_access_token"]
        expire = int(data.get("expire", 7200))

        # 比飞书实际过期时间提前5分钟清除
        cache_ttl = max(expire - 300, 60)
        await set_cache(access_token_key, token, cache_ttl)

        return token

async def _send_feishu_reply(message_id: str, text: str):
    """回复飞书消息（基于原消息id进行回复）"""
    token = await _get_tenant_access_token()
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{FEISHU_API_BASE}/im/v1/messages/{message_id}/reply",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "content": json.dumps({"text": text}, ensure_ascii=False),
                "msg_type": "text",
            },
        )

    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(
            f"飞书回复失败: code={data.get('code')}, msg={data.get('msg')}"
        )

    return data


async def _verify_signature(request_token: str, verification_token: str) -> bool:
    """
    使用 Verification Token 校验请求来源（适用于未开启 Encrypt Key 的场景）
    """
    try:
        if request_token is None:
            return False
        return request_token == verification_token
    except Exception as e:
        print(f"Error verifying signature: {e}")
        return False


async def get_feishu_user_detail(feishu_open_id: str):
    token = await _get_tenant_access_token()

    url = (
        f"https://open.feishu.cn/open-apis/contact/v3/users/"
        f"{feishu_open_id}"
    )

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            params={"user_id_type": "open_id",
                    "department_id_type": "department_id"},
        )
        resp.raise_for_status()
        data = resp.json()

    if data.get("code") != 0:
        raise Exception(f"获取用户信息失败: {data.get('msg')}")

    user = data.get("data", {}).get("user", {})

    return {
        "user_id": user.get("user_id"),
        "name": user.get("name"),
        "i18n_name": user.get("i18n_name"),
        "department_ids": user.get("department_ids", []),
    }


async def get_feishu_department_detail(
    department_id: int | str,
) -> dict:
    """根据飞书部门 ID 获取部门详情。"""

    token = await _get_tenant_access_token()

    url = (
        "https://open.feishu.cn/open-apis/contact/v3/departments/"
        f"{department_id}"
    )

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }

    params = {
        # 使用飞书内部数字部门 ID
        "department_id_type": "department_id",

        # 部门负责人等用户字段使用 open_id
        "user_id_type": "open_id",
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            url,
            headers=headers,
            params=params,
        )
        resp.raise_for_status()
        data = resp.json()

    if data.get("code") != 0:
        raise RuntimeError(
            f"获取飞书部门信息失败："
            f"code={data.get('code')}, msg={data.get('msg')}"
        )

    department = data.get("data", {}).get("department")
    if not department:
        raise RuntimeError(
            f"飞书未返回部门信息：department_id={department_id}"
        )

    return department
