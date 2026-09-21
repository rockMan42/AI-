import asyncio
import time

from pydantic import ValidationError
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert

from app.config.settings import get_settings
from app.core import redis_client as redis_module
from app.core.database import create_session
from app.models.department import Department
from app.models.permission import (
    OrganizationState,
    RoleMappingRule,
    UserRole,
)
from app.models.user import User
from app.schemas.permission import (
    AccessDenied,
    PermissionUnavailable,
    Principal,
    Role,
)


def map_role(open_id: str, snapshot: dict, rules) -> tuple[Role, str, set[str]]:
    member = snapshot["users"][open_id]
    departments = set(member["departments"])
    managed_departments = set(snapshot["leaders"].get(open_id, []))

    managed = {
        target_id
        for target_id, target in snapshot["users"].items()
        if target_id != open_id and target["active"] and (
            target["leader"] == open_id
            or managed_departments.intersection(target["departments"])
        )
    }

    manual = next(
        (
            rule for rule in rules
            if rule.enabled and rule.kind == "user" and rule.subject == open_id
        ),
        None,
    )
    if manual:
        return Role(manual.role), "manual", managed

    is_hr = any(
        rule.enabled
        and rule.kind == "department"
        and rule.subject in departments
        and rule.role == Role.HR_ADMIN
        for rule in rules
    )

    if is_hr:
        return Role.HR_ADMIN, "auto", managed

    if managed_departments or managed:
        return Role.MANAGER, "auto", managed

    return Role.EMPLOYEE, "auto", set()


async def local_department(db, department_id: str, snapshot: dict, seen=None):
    seen = set() if seen is None else seen
    if department_id in seen:
        raise PermissionUnavailable("部门层级存在循环")
    seen.add(department_id)

    existing = await db.scalar(
        select(Department).where(
            Department.feishu_department_id == department_id
        )
    )
    if existing:
        return existing.department_id

    detail = snapshot["departments"].get(department_id)
    if detail is None:
        raise PermissionUnavailable("部门不在已同步的通讯录范围内")

    parent = detail["parent_id"]
    parent_id = None

    if parent not in (None, "", "0"):
        parent_id = await local_department(db, str(parent), snapshot, seen)

    statement = insert(Department).values(
        feishu_department_id=department_id,
        name=detail["name"],
        parent_id=parent_id,
        manager_user_id=None,
        sort_order=0,
    )
    await db.execute(statement.on_duplicate_key_update(
        feishu_department_id=statement.inserted.feishu_department_id,
    ))

    return await db.scalar(
        select(Department.department_id).where(
            Department.feishu_department_id == department_id
        )
    )


async def read_cache(key: str) -> Principal | None:
    client = redis_module.redis_client
    if client is None:
        return None

    try:
        async with asyncio.timeout(0.05):
            value = await client.get(key)
        return Principal.model_validate_json(value) if value else None
    except (RedisError, TimeoutError, ValidationError):
        return None


async def write_cache(key: str, principal: Principal):
    client = redis_module.redis_client
    if client is None:
        return

    try:
        async with asyncio.timeout(0.05):
            await client.set(
                key,
                principal.model_dump_json(),
                ex=get_settings().permission_cache_ttl,
            )
    except (RedisError, TimeoutError):
        pass


async def resolve_principal(open_id: str) -> Principal:
    from app.security.auth import ACTIVE_STATUSES

    settings = get_settings()

    async with create_session() as db, db.begin():
        actor = await db.scalar(
            select(User).where(User.feishu_open_id == open_id)
        )
        if actor is None or actor.status not in ACTIVE_STATUSES:
            raise AccessDenied("用户不存在或已停用")

        state_info = (
            await db.execute(
                select(
                    OrganizationState.version,
                    OrganizationState.dirty,
                    OrganizationState.synced_at,
                ).where(OrganizationState.id == 1)
            )
        ).one_or_none()

        if (
            state_info is None
            or state_info.dirty
            or time.time() - state_info.synced_at > settings.permission_org_max_age
        ):
            raise PermissionUnavailable("组织权限正在同步，请稍后重试")

        version = state_info.version
        key = f"dep:permission:role:{version}:{actor.user_id}"
        principal = await read_cache(key)

        if principal and (
            principal.user_id == actor.user_id
            and principal.open_id == open_id
            and principal.version == version
        ):
            return principal

        saved = await db.get(UserRole, actor.user_id)
        if saved and saved.version == version:
            principal = Principal.model_validate(saved.profile)
        else:
            snapshot = await db.scalar(
                select(OrganizationState.snapshot)
                .where(OrganizationState.id == 1)
            )
            member = snapshot["users"].get(open_id)

            if not member or not member["active"]:
                raise AccessDenied("用户不在有效通讯录范围内")

            rules = (
                await db.scalars(select(RoleMappingRule))
            ).all()

            role, source, managed_open_ids = map_role(open_id, snapshot, rules)

            managed_ids = ()
            if role == Role.MANAGER and managed_open_ids:
                managed_ids = tuple(
                    await db.scalars(
                        select(User.user_id).where(
                            User.feishu_open_id.in_(managed_open_ids),
                            User.status.in_(ACTIVE_STATUSES),
                        )
                    )
                )

            actor.department_id = await local_department(
                db, member["departments"][0], snapshot,
            )

            principal = Principal(
                user_id=actor.user_id,
                open_id=open_id,
                role=role,
                department_id=actor.department_id,
                managed_user_ids=managed_ids,
                source=source,
                version=version,
            )

            # 当前读锁阻止事件在提交角色快照时改变版本。
            current = await db.scalar(
                select(OrganizationState.version)
                .where(OrganizationState.id == 1)
                .with_for_update(read=True)
            )
            if current != version:
                raise PermissionUnavailable("组织权限已变化，请重试")

            values = {
                "user_id": actor.user_id,
                "feishu_user_id": member["feishu_user_id"],
                "feishu_open_id": open_id,
                "role": role.value,
                "role_source": source,
                "version": version,
                "profile": principal.model_dump(mode="json"),
            }

            statement = insert(UserRole).values(**values)
            await db.execute(statement.on_duplicate_key_update(
                **{key: value for key, value in values.items() if key != "user_id"}
            ))

    await write_cache(key, principal)
    return principal
