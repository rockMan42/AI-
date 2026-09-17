import json
import asyncio
from app.core.rag_context import timed

import httpx
from app.config.settings import get_settings
from app.core.redis_client import get_cache, set_cache
from uuid import uuid4
from app.core import redis_client as redis_module

_client: httpx.AsyncClient | None = None
_token_lock: asyncio.Lock | None = None


async def init_feishu_client():
    global _client, _token_lock
    if _client is None:
        _client = httpx.AsyncClient(
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            timeout=httpx.Timeout(5.0, connect=1.0, pool=1.0),
        )
        _token_lock = asyncio.Lock()


async def close_feishu_client():
    global _client, _token_lock
    client, _client = _client, None
    _token_lock = None
    if client is not None:
        await client.aclose()


def get_feishu_client():
    if _client is None:
        raise RuntimeError("飞书客户端尚未初始化")
    return _client


# 飞书 API 基础地址
FEISHU_API_BASE = "https://open.feishu.cn/open-apis"
access_token_key = "dep:tenant_access_token"

@timed("feishu_token")
async def _get_tenant_access_token() -> str:
    token = await get_cache(access_token_key)
    if token is not None:
        return token
    if _token_lock is None:
        raise RuntimeError("飞书客户端尚未初始化")
    async with _token_lock:
        # 原函数会在锁内再次读缓存。
        return await _fetch_tenant_access_token()


async def _fetch_tenant_access_token() -> str:
    """获取飞书用户访问令牌（自动缓存和续期）"""

    # 从缓存中获取令牌
    token = await get_cache(access_token_key)
    if token is not None:
        return token

    settings = get_settings()
    client = get_feishu_client()
    res = await client.post(
        f"{FEISHU_API_BASE}/auth/v3/tenant_access_token/internal",
        json={
            "app_id": settings.feishu_app_id,
            "app_secret": settings.feishu_app_secret,
        },
    )

    res.raise_for_status()
    data = res.json()
    if data.get("code") != 0:
        raise RuntimeError("获取飞书令牌失败")

    token = data["tenant_access_token"]
    expire = int(data.get("expire", 7200))

    # 比飞书实际过期时间提前5分钟清除
    cache_ttl = max(expire - 300, 60)
    await set_cache(access_token_key, token, cache_ttl)

    return token

@timed("feishu_send")
async def _send_feishu_reply(message_id: str, text: str):
    """回复飞书消息（基于原消息id进行回复）"""
    token = await _get_tenant_access_token()
    client = get_feishu_client()
    await wait_feishu_send_slot()
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

    client = get_feishu_client()
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

    client = get_feishu_client()
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

@timed("feishu_send")
async def _send_feishu_card_reply(
    message_id: str,
    card: dict,
):
    token = await _get_tenant_access_token()

    client = get_feishu_client()
    await wait_feishu_send_slot()
    resp = await client.post(
        f"{FEISHU_API_BASE}/im/v1/messages/{message_id}/reply",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        },
    )

    resp.raise_for_status()
    payload = resp.json()

    if payload.get("code") != 0:
        # 不把返回正文或卡片内容写入异常。
        raise RuntimeError("飞书卡片发送失败")

    return payload


SEND_LIMIT_SCRIPT = """
local clock = redis.call('TIME')
local now = clock[1] * 1000 + math.floor(clock[2] / 1000)

redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - 60000)

local second_count = redis.call(
    'ZCOUNT', KEYS[1], now - 1100, '+inf'
)
local minute_count = redis.call('ZCARD', KEYS[1])

if second_count >= 50 then
    return 100
end
if minute_count >= 1000 then
    return 1000
end

redis.call('ZADD', KEYS[1], now, ARGV[1])
redis.call('PEXPIRE', KEYS[1], 61000)
return 0
"""


async def wait_feishu_send_slot():
    client = redis_module.redis_client
    if client is None:
        raise RuntimeError("Redis 尚未初始化")

    key = f"dep:feishu:send-limit:{get_settings().feishu_app_id}"

    while True:
        delay = int(
            await client.eval(
                SEND_LIMIT_SCRIPT, 1, key, uuid4().hex,
            )
        )
        if delay == 0:
            return
        await asyncio.sleep(delay / 1000)


"""
    这里的限流键供多个进程共享，防止两个通知同时推送时各自发送五十条。额外保留每分钟上限的约束。
"""
async def send_feishu_card(
    open_id: str,
    card: dict,
    message_uuid: str,
) -> str:
    token = await _get_tenant_access_token()
    await wait_feishu_send_slot()

    response = await get_feishu_client().post(
        f"{FEISHU_API_BASE}/im/v1/messages",
        params={"receive_id_type": "open_id"},
        headers={"Authorization": f"Bearer {token}"},
        json={
            "receive_id": open_id,
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
            "uuid": message_uuid,
        },
    )
    response.raise_for_status()

    data = response.json()
    if data.get("code") != 0:
        raise RuntimeError("飞书通知发送失败")

    message_id = (data.get("data") or {}).get("message_id")
    if not message_id:
        raise RuntimeError("飞书未返回消息编号")
    return message_id


async def update_feishu_card(message_id: str, card: dict):
    token = await _get_tenant_access_token()
    await wait_feishu_send_slot()
    response = await get_feishu_client().patch(
        f"{FEISHU_API_BASE}/im/v1/messages/{message_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"content": json.dumps(card, ensure_ascii=False)},
    )
    response.raise_for_status()
    data = response.json()
    if data.get("code") != 0:
        raise RuntimeError(f"飞书卡片更新失败 code={data.get('code')}")
