from sqlalchemy import false, true

from app.schemas.permission import AccessDenied, Principal, Role
from app.services.permission_audit import record_audit


READ_ACTIONS = {
    "attendance.read",
    "performance.read",
    "salary.read",
}

ADMIN_ACTIONS = {
    "rules.read",
    "rules.write",
    "rules.rollback",
    "role.read",
    "role.write",
    "audit.read",
    "performance.report",
    "performance.remind",
    "notification.manage",
}


def permissions(principal: Principal) -> dict[str, str]:
    scope = {
        Role.EMPLOYEE: "self",
        Role.MANAGER: "team",
        Role.HR_ADMIN: "company",
    }[principal.role]

    result = {action: scope for action in READ_ACTIONS}
    if principal.role == Role.HR_ADMIN:
        result.update({action: "company" for action in ADMIN_ACTIONS})
    return result


def can_access(
    principal: Principal,
    action: str,
    target_user_id: int | None = None,
    scope: str | None = None,
) -> bool:
    if action in ADMIN_ACTIONS:
        return principal.role == Role.HR_ADMIN

    if action not in READ_ACTIONS:
        return False

    if scope not in {None, "self", "team", "company"}:
        return False

    if scope == "company" and principal.role != Role.HR_ADMIN:
        return False

    if scope == "team" and principal.role == Role.EMPLOYEE:
        return False

    if target_user_id is None or target_user_id == principal.user_id:
        return True

    if principal.role == Role.HR_ADMIN:
        return True

    return (
        principal.role == Role.MANAGER
        and target_user_id in principal.managed_user_ids
    )


async def authorize(
    principal: Principal,
    action: str,
    target_user_id: int | None = None,
    scope: str | None = None,
):
    allowed = can_access(principal, action, target_user_id, scope)

    await record_audit(
        principal.user_id,
        action,
        allowed,
        target_user_id,
        {"scope": scope, "role": principal.role.value},
    )

    if not allowed:
        raise AccessDenied("无权执行该操作")


def scope_condition(principal: Principal, owner_column):
    if principal.role == Role.HR_ADMIN:
        return true()

    if principal.role == Role.MANAGER:
        return owner_column.in_({
            principal.user_id,
            *principal.managed_user_ids,
        })

    if principal.role == Role.EMPLOYEE:
        return owner_column == principal.user_id

    return false()
