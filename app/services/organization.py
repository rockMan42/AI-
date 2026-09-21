import asyncio
import time
from collections import defaultdict
from contextlib import suppress

from redis.exceptions import RedisError
from sqlalchemy import select

from app.core import redis_client as redis_module
from app.core.database import create_session
from app.models.permission import OrganizationState
from app.schemas.permission import PermissionUnavailable
from app.services.conversation_engine.feishu import (
    FEISHU_API_BASE,
    _get_tenant_access_token,
    get_feishu_client,
)

"""
同步组织架构，解析并缓存角色
"""

async def contact_get(path: str, **params) -> dict:
    client = get_feishu_client()
    token = await _get_tenant_access_token()

    for attempt in range(3):
        await asyncio.sleep(0.12)

        response = await client.get(
            f"{FEISHU_API_BASE}/contact/v3/{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
        )

        if response.status_code == 429:
            await asyncio.sleep(2 ** attempt)
            continue

        response.raise_for_status()
        payload = response.json()

        if payload.get("code") != 0:
            raise PermissionUnavailable("通讯录读取失败")

        return payload["data"]

    raise PermissionUnavailable("通讯录请求过于频繁")


async def contact_pages(path: str, **params) -> list[dict]:
    items = []
    seen_tokens = set()

    while True:
        data = await contact_get(path, page_size=50, **params)
        items.extend(data.get("items") or [])

        if not data.get("has_more"):
            return items

        page_token = data.get("page_token")
        if not page_token or page_token in seen_tokens:
            raise PermissionUnavailable("通讯录分页异常")

        seen_tokens.add(page_token)
        params["page_token"] = page_token


async def read_organization() -> dict:
    common = {
        "department_id_type": "department_id",
        "user_id_type": "open_id",
    }

    departments = await contact_pages(
        "departments/0/children",
        fetch_child="true",
        **common,
    )

    departments.insert(0, {
        "department_id": "0",
        "name": "全公司",
        "parent_department_id": None,
    })

    users = {}
    leaders = defaultdict(list)
    department_map = {}

    for department in departments:
        department_id = str(department["department_id"])
        department_map[department_id] = {
            "name": department["name"],
            "parent_id": department.get("parent_department_id"),
        }

        leader = department.get("leader_user_id")
        if leader:
            leaders[leader].append(department_id)

        members = await contact_pages(
            "users/find_by_department",
            department_id=department_id,
            **common,
        )

        for member in members:
            open_id = member.get("open_id")
            status = member.get("status")
            department_ids = member.get("department_ids")

            missing = [
                name
                for name, value in {
                    "open_id": open_id,
                    "status": status if isinstance(status, dict) else None,
                    "department_ids": department_ids,
                }.items()
                if not value
            ]
            if missing:
                raise PermissionUnavailable(
                    "通讯录缺少字段："
                    f"{missing}，实际字段：{sorted(member)}"
                )

            users[open_id] = {
                "feishu_user_id": member.get("user_id"),
                "departments": [str(value) for value in department_ids],
                "leader": member.get("leader_user_id") or "",
                "active": (
                    status.get("is_activated") is True
                    and not status.get("is_frozen")
                    and not status.get("is_resigned")
                    and not status.get("is_exited")
                ),
            }

    return {
        "users": users,
        "leaders": dict(leaders),
        "departments": department_map,
    }


async def sync_organization():
    client = redis_module.redis_client
    if client is None:
        raise PermissionUnavailable("Redis 尚未初始化")

    lock = client.lock(
        "dep:permission:organization-sync",
        timeout=120,
        blocking=False,
        thread_local=False,
    )

    if not await lock.acquire():
        return

    try:
        async with asyncio.timeout(110):
            async with create_session() as db:
                version = await db.scalar(
                    select(OrganizationState.version)
                    .where(OrganizationState.id == 1)
                )

            snapshot = await read_organization()

            async with create_session() as db, db.begin():
                state = await db.scalar(
                    select(OrganizationState)
                    .where(OrganizationState.id == 1)
                    .with_for_update()
                )

                if state is None:
                    raise PermissionUnavailable("权限表尚未初始化")

                # 同步期间出现新事件时，丢弃本轮快照。
                if state.version != version:
                    return

                state.snapshot = snapshot
                state.version += 1
                state.dirty = False
                state.synced_at = int(time.time())
    finally:
        with suppress(RedisError):
            await lock.release()
