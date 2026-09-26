from types import SimpleNamespace


from app.models.user import User
from app.models.department import Department
from app.security.auth import ACTIVE_STATUSES
from app.services.business_rules.common import RuleError
from app.services.business_rules.service import read_current
from app.services.business_rules.evaluators import evaluate_snapshot


async def locked_expense_policy(db):
    # 所有提交均先 approval 后 reimbursement，发布只锁一类，避免锁顺序倒置。
    approval = await read_current(db, "approval", lock=True)
    reimbursement = await read_current(db, "reimbursement", lock=True)
    return SimpleNamespace(
        revision=reimbursement["version"],
        config_json=reimbursement["rule_data"]["policy"],
        rules_snapshot=reimbursement,
        approval_snapshot=approval,
    )


async def resolve_route(db, snapshot, user, amount):
    result = evaluate_snapshot(
        snapshot, {"request_type": "reimbursement", "amount": amount}
    )
    department = await db.get(Department, user.department_id)
    manager = department.manager_user_id if department else None
    nodes = []
    for node in result["nodes"]:
        identifier = (
            manager
            if node["actor"]["kind"] == "direct_manager"
            else node["actor"]["user_id"]
        )
        actor = await db.get(User, identifier) if identifier else None
        if (
            not actor
            or actor.status not in ACTIVE_STATUSES
            or actor.user_id == user.user_id
        ):
            raise RuleError(
                "审批人不存在、已停用或与申请人相同", 409, "APPROVER_INVALID"
            )
        nodes.append(
            {"id": node["id"], "stage": node["stage"], "user_id": actor.user_id}
        )
    if len({n["user_id"] for n in nodes}) != len(nodes):
        raise RuleError(
            "主管、财务、出纳不能由同一人兼任本单审批", 409, "APPROVER_INVALID"
        )
    return {
        "route_id": result["route_id"],
        "rule_version": snapshot["version"],
        "nodes": nodes,
    }


async def validate_fixed_route(
    db, expense, to_status=None, next_id=None, *, current_status=None
):
    route = (expense.payload or {}).get("approval_route")
    if route is None:
        # 已提交的历史单据保留原流程。
        return
    nodes = route["nodes"]
    if [n["stage"] for n in nodes] != ["manager", "finance", "cashier"]:
        raise RuleError("审批快照不兼容", 409)
    if to_status is None:
        stage_index = {
            "submitted": 0,
            "manager_approved": 1,
            "finance_approved": 2,
        }.get(current_status, 0)
        for node in nodes[stage_index:]:
            actor = await db.get(User, node["user_id"])
            if (
                not actor
                or actor.status not in ACTIVE_STATUSES
                or actor.user_id == expense.user_id
            ):
                raise RuleError(
                    "固定审批路线中的人员已失效，需要人工处理", 409, "APPROVER_INVALID"
                )
        return
    expected_stage = {"manager_approved": 1, "finance_approved": 2}.get(to_status)
    expected_next = (
        nodes[expected_stage]["user_id"] if expected_stage is not None else None
    )
    if next_id != expected_next:
        raise RuleError("下一审批人与已确认路线不一致", 409, "APPROVAL_ROUTE_MISMATCH")


async def legacy_principal(user_id):
    from app.core.database import create_session
    from app.services.role_mapper import resolve_principal

    async with create_session() as db:
        user = await db.get(User, user_id)
        if not user:
            raise RuleError("用户不存在", 403)
        open_id = user.feishu_open_id
    return await resolve_principal(open_id)


async def legacy_update(user_id, rule_id=None, rule=None, policy=None):
    from uuid import uuid4
    from app.schemas.business_rule import RuleUpdate
    from app.services.business_rules.service import RuleService

    service = RuleService()
    actor = await legacy_principal(user_id)
    snapshot = await service.get(actor, "reimbursement")
    data = snapshot["rule_data"]
    if policy is not None:
        data["policy"] = policy.model_dump(mode="json")
    else:
        rules = data["rules"]
        identifier = (
            rule_id
            if rule_id is not None
            else max((r["id"] for r in rules), default=0) + 1
        )
        replacement = {"id": identifier, **rule.model_dump(mode="json")}
        found = next((i for i, r in enumerate(rules) if r["id"] == identifier), None)
        if rule_id is not None and found is None:
            raise RuleError("规则不存在", 404)
        if found is None:
            rules.append(replacement)
        else:
            rules[found] = replacement
    body = RuleUpdate(
        expected_version=snapshot["version"],
        rule_data=data,
        bindings=snapshot["bindings"],
        change_summary="通过兼容报销管理接口修改",
    )
    result = await service.publish(
        actor, "reimbursement", body, "legacy:" + uuid4().hex
    )
    return {
        **result,
        **(
            {"revision": result["version"], **data["policy"]}
            if policy is not None
            else replacement
        ),
    }
