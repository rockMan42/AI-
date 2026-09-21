from types import SimpleNamespace as NS

import pytest

from app.schemas.permission import Principal, Role, RuleInput
from app.security.permission import can_access, permissions
from app.services.role_mapper import map_role


def principal(role: Role, managed=()) -> Principal:
    return Principal(
        user_id=3,
        open_id="actor",
        role=role,
        department_id=1,
        managed_user_ids=managed,
        version=1,
    )


def snapshot(*, departments=("dept",), managed=False) -> dict:
    users = {
        "actor": {
            "departments": list(departments),
            "leader": "",
            "active": True,
        },
        "member": {
            "departments": ["dept"],
            "leader": "actor" if managed else "",
            "active": True,
        },
    }
    return {
        "users": users,
        "leaders": {"actor": ["dept"]} if managed else {},
        "departments": {},
    }


def test_manual_user_rule_has_highest_priority():
    rules = [NS(
        enabled=True,
        kind="user",
        subject="actor",
        role="Manager",
    )]

    role, source, managed = map_role("actor", snapshot(), rules)

    assert (role, source, managed) == (Role.MANAGER, "manual", set())


def test_department_rule_maps_hr():
    rules = [NS(
        enabled=True,
        kind="department",
        subject="dept",
        role=Role.HR_ADMIN,
    )]

    role, source, _ = map_role("actor", snapshot(), rules)

    assert (role, source) == (Role.HR_ADMIN, "auto")


def test_reporting_line_maps_manager_and_active_members():
    role, source, managed = map_role(
        "actor",
        snapshot(managed=True),
        [],
    )

    assert (role, source) == (Role.MANAGER, "auto")
    assert managed == {"member"}


@pytest.mark.parametrize(
    "role,target,allowed",
    [
        (Role.EMPLOYEE, 3, True),
        (Role.EMPLOYEE, 4, False),
        (Role.MANAGER, 4, True),
        (Role.MANAGER, 5, False),
        (Role.HR_ADMIN, 5, True),
    ],
)
def test_read_scope(role, target, allowed):
    actor = principal(
        role,
        managed=(4,) if role == Role.MANAGER else (),
    )

    assert can_access(actor, "attendance.read", target) is allowed


def test_only_hr_has_admin_permissions():
    assert "role.write" not in permissions(principal(Role.EMPLOYEE))
    assert "role.write" not in permissions(principal(Role.MANAGER))
    assert permissions(principal(Role.HR_ADMIN))["role.write"] == "company"
    assert not can_access(principal(Role.MANAGER), "role.write")
    assert can_access(principal(Role.HR_ADMIN), "role.write")


def test_department_rule_only_accepts_hr_role():
    with pytest.raises(ValueError, match="部门规则仅用于指定 HR 部门"):
        RuleInput(
            kind="department",
            subject="dept",
            role=Role.MANAGER,
        )
