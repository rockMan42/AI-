from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from app.services.expense.approval_state import (
    POLL_STATUSES,
    REJECTED_STATUSES,
)
from app.core.database import create_session
from app.models import Department, Expense, ExpenseItem, Invoice, User
from app.models.expense_flow import (
    BusinessTrip,
    ExpenseInvoiceClaim,
    ExpensePolicy,
)
from app.schemas.expense import (
    ExpenseCheckInput,
    ExpensePolicyInput,
    ExpensePrepareInput,
    ExpenseRuleInput,
    ExpenseSupplement,
)
from app.security.auth import ACTIVE_STATUSES
from app.services.expense.rules import (
    ExpenseError, check_facts, digest, failure,
    invoice_key, load_rules, money,
)
from app.utils.time import as_shanghai, utc_now

"""
规则更新、草稿生成、提交复核
"""

CATEGORY = {
    "hotel": "accommodation",
    "meal": "meals",
    "train": "transport",
    "flight": "transport",
    "taxi": "transport",
    "other": "other",
}

ACTIVE_EXPENSE_STATUSES = {
    "draft",
    "submitting",
    "submitted",
    "manager_approved",
    "finance_approved",
    "manager_rejected",
    "finance_rejected",
    "rejected",
    "approved",  # 保留历史数据的防重复校验。
    "paid",
}

def detail_data(row: Expense) -> dict:
    payload = row.payload or {}
    return {
        "expense_id": row.id,
        "expense_no": row.expense_no,
        "trip_id": row.trip_id,
        "total_amount": str(row.total_amount),
        "breakdown": payload.get("breakdown", {}),
        "status": row.status,
        "has_override": bool(row.has_override),
        "override_reason": row.override_reason,
        "finance_no": row.finance_no,
        "submitted_at": (
            as_shanghai(row.submitted_at).isoformat()
            if row.submitted_at else None
        ),
        "last_error": row.last_error,
        "results": payload.get("results", []),
    }

async def locked_policy(db):
    from app.config.settings import get_settings
    if get_settings().business_rules_enabled:
        from app.services.business_rules.expense_adapter import locked_expense_policy
        return await locked_expense_policy(db)
    policy = await db.scalar(
        select(ExpensePolicy)
        .where(ExpensePolicy.id == 1)
        .with_for_update()
    )
    if policy is None:
        raise ExpenseError("报销基础配置未初始化", 503)
    return policy

async def locked_user(db, user_id):
    user = await db.scalar(
        select(User)
        .where(User.user_id == user_id)
        .with_for_update()
    )
    if user is None or user.status not in ACTIVE_STATUSES:
        raise ExpenseError("用户不存在或已停用", 403)
    return user

async def owned_expense(db, user_id, expense_id, *, lock=False):
    query = select(Expense).where(Expense.id == expense_id)
    if lock:
        query = query.with_for_update()

    row = await db.scalar(query)

    if row is None:
        raise ExpenseError("报销单不存在", 404)

    if row.user_id != user_id:
        raise ExpenseError("无权操作该报销单", 403)

    return row

class ExpenseService:
    async def supplement(self,
                         user_id: int,
                         invoice_id: int,
                         body: ExpenseSupplement
                         ):
        async with create_session() as db, db.begin():
            await locked_user(db, user_id)
            row = await db.scalar(
                select(Invoice)
                .where(Invoice.id == invoice_id)
                .with_for_update()
            )
            if row is None:
                raise ExpenseError("发票不存在", 404)

            payload = dict(row.ocr_result_json or {})
            if payload.get("_meta", {}).get("user_id") != user_id:
                raise ExpenseError("无权操作该发票", 403)
            if row.expense_item_id is not None:
                raise ExpenseError("请先取消对应草稿，再修改报销信息")

            # 不修改 OCR 原始字段，单独保存业务补充信息。
            payload["_expense"] = body.model_dump(mode="json")
            row.ocr_result_json = payload

        return {"invoice_id": invoice_id, **body.model_dump(mode="json")}

    async def _evaluate(self,
                        db,
                        user,
                        policy,
                        body: ExpenseCheckInput,
                        current_expense_id: int | None = None):
        """
        报销单审核
        :param db:
        :param user:
        :param policy:
        :param body:
        :param current_expense_id:
        :return:
        """
        config = policy.config_json
        if not config.get("company_name") or not config.get("company_tax_id"):
            raise ExpenseError("请管理员先配置公司全称和税号", 409)

        trip = None
        if body.trip_id is not None:
            trip = await db.scalar(
                select(BusinessTrip)
                .where(BusinessTrip.id == body.trip_id)
                .with_for_update()
            )
            if trip is None:
                raise ExpenseError("出差申请不存在", 404)
            if trip.user_id != user.user_id:
                raise ExpenseError("不能使用他人的出差申请", 403)
            if trip.status not in {"approved", "completed"}:
                raise ExpenseError("只能关联审批通过或已完成的出差申请")
            if trip.end_date < trip.start_date:
                raise ExpenseError("出差申请日期异常")

        rows = list((
                        await db.scalars(
                            select(Invoice)
                            .where(Invoice.id.in_(body.invoice_ids))
                            .order_by(Invoice.id)
                            .with_for_update()
                        )
                    ).all())

        if len(rows) != len(body.invoice_ids):
            raise ExpenseError("部分发票不存在", 404)

        rules = ([r for r in policy.rules_snapshot["rule_data"]["rules"] if r["status"] == "active"]
                 if hasattr(policy, "rules_snapshot") else await load_rules(db, policy.revision))
        today = as_shanghai(utc_now()).date()
        previous_meals = Decimal("0.00")

        if trip:
            query = (
                select(func.coalesce(func.sum(ExpenseItem.amount), 0))
                .join(Expense, Expense.id == ExpenseItem.expense_id)
                .where(
                    Expense.user_id == user.user_id,
                    Expense.trip_id == trip.id,
                    Expense.status.in_(ACTIVE_EXPENSE_STATUSES),
                    Expense.correction_opened_at.is_(None),
                    ExpenseItem.category.in_(["meals", "餐饮"]),
                )
            )
            if current_expense_id is not None:
                query = query.where(Expense.id != current_expense_id)
            previous_meals = Decimal(str(await db.scalar(query)))

        items = []
        results = []
        seen = set()

        for row in rows:
            ocr = row.ocr_result_json or {}
            if ocr.get("_meta", {}).get("user_id") != user.user_id:
                raise ExpenseError("包含不属于当前用户的发票", 403)

            failures = []
            if not row.verified:
                failures.append(failure("人工确认", "请先确认 OCR 识别结果"))

            if row.expense_item_id:
                linked_expense = await db.scalar(
                    select(ExpenseItem.expense_id).where(
                        ExpenseItem.id == row.expense_item_id,
                    )
                )
                if linked_expense != current_expense_id:
                    failures.append(
                        failure("重复提交", "该发票已经关联其他报销单")
                    )

            try:
                supplement = ExpenseSupplement.model_validate(
                    ocr.get("_expense") or {},
                )
                amount = money(ocr.get("total_amount"))
                issued = date.fromisoformat(str(ocr.get("invoice_date")))
                key = invoice_key(
                    ocr.get("invoice_code"),
                    ocr.get("invoice_number"),
                )
            except (ValueError, ValidationError) as exc:
                failures.append(
                    failure("必要信息", f"发票信息不完整：{exc}")
                )
                results.append({
                    "invoice_id": row.id,
                    "passed": False,
                    "failures": failures,
                })
                continue

            if key in seen:
                failures.append(
                    failure("重复提交", "本次选择中存在重复发票")
                )
            seen.add(key)

            claim = await db.get(ExpenseInvoiceClaim, key)
            if claim and claim.expense_id != current_expense_id:
                failures.append(
                    failure("重复提交", "发票已被其他报销单占用")
                )

            # 兼容迁移前没有 claim 记录的历史报销。
            # 历史记录缺少代码时按号码保守拦截，交给财务核实。
            old_query = (
                select(ExpenseItem.id, Invoice.ocr_result_json)
                .join(Expense, Expense.id == ExpenseItem.expense_id)
                .outerjoin(
                    Invoice,
                    Invoice.expense_item_id == ExpenseItem.id,
                )
                .where(
                    ExpenseItem.invoice_no == str(ocr["invoice_number"]).strip(),
                    Expense.status.in_(ACTIVE_EXPENSE_STATUSES),
                    Expense.correction_opened_at.is_(None),
                )
            )
            if current_expense_id is not None:
                old_query = old_query.where(
                    Expense.id != current_expense_id,
                )

            historical_duplicate = False
            for _, old_ocr in (await db.execute(old_query)).all():
                old_code = (old_ocr or {}).get("invoice_code")
                if not old_code or str(old_code).strip() == str(
                        ocr.get("invoice_code") or ""
                ).strip():
                    historical_duplicate = True
                    break

            if historical_duplicate:
                failures.append(
                    failure("重复提交", "历史报销中存在相同发票号码")
                )

            item = {
                "invoice_id": row.id,
                "invoice_key": key,
                "invoice_number": str(ocr["invoice_number"]).strip(),
                "invoice_date": issued.isoformat(),
                "invoice_code": str(ocr.get("invoice_code") or "").strip(),
                "amount": str(amount),
                "category": CATEGORY[supplement.expense_type],
                "description": str(ocr.get("items_description") or "")[:500],
                "image_url": row.image_url,
                "buyer_name": ocr.get("buyer_name"),
                "buyer_tax_id": ocr.get("buyer_tax_id"),
                **supplement.model_dump(mode="json"),
            }
            items.append(item)
            results.append({
                "invoice_id": row.id,
                "invoice_number": item["invoice_number"],
                "passed": False,
                "failures": failures,
            })

        current_meals = sum(
            (
                Decimal(item["amount"])
                for item in items if item["category"] == "meals"
            ),
            Decimal("0.00"),
        )

        days = (trip.end_date - trip.start_date).days + 1 if trip else 1
        tolerance = timedelta(days=config.get("date_tolerance_days", 1))
        trip_window = (
            [
                (trip.start_date - tolerance).isoformat(),
                (trip.end_date + tolerance).isoformat(),
            ]
            if trip else None
        )

        position = config.get("position_map", {}).get(
            user.position_level or user.role,
        )
        by_id = {result["invoice_id"]: result for result in results}
        facts_list = []

        for item in items:
            city = (item.get("city") or "").strip().removesuffix("市")
            nights = None
            if item["expense_type"] == "hotel":
                start = date.fromisoformat(item["check_in"])
                end = date.fromisoformat(item["check_out"])
                nights = (end - start).days

                if trip and not (
                        trip.start_date - tolerance <= start
                        and end <= trip.end_date + tolerance
                ):
                    by_id[item["invoice_id"]]["failures"].append(
                        failure("住宿日期", "入住或退房日期超出允许范围")
                    )

            facts = {
                **item,
                "city": city,
                "has_trip": trip is not None,
                "city_tier": (
                    "tier1"
                    if city in config.get("tier1_cities", [])
                    else "tier2"
                ),
                "position_level": position,
                "company_name": config["company_name"],
                "company_tax_id": config["company_tax_id"],
                "trip_window": trip_window,
                "nightly_amount": (
                    str(Decimal(item["amount"]) / nights)
                    if nights else None
                ),
                "meal_daily_amount": str(
                    (previous_meals + current_meals) / days
                ),
            }

            if item["expense_type"] in {"train", "flight"} and not position:
                by_id[item["invoice_id"]]["failures"].append(
                    failure(
                        "职级映射",
                        "员工职级尚未映射到报销标准",
                        "请管理员补充职级映射",
                    )
                )

            by_id[item["invoice_id"]]["failures"].extend(
                check_facts(rules, facts, today)
            )
            facts_list.append(facts)

        breakdown = {}
        for item in items:
            category = item["category"]
            breakdown[category] = (
                    breakdown.get(category, Decimal("0.00"))
                    + Decimal(item["amount"])
            )

        total = sum(breakdown.values(), Decimal("0.00"))
        if total > Decimal("9999999999.99"):
            raise ExpenseError("报销总金额超过允许范围", 422)

        for result in results:
            result["passed"] = not any(
                item["severity"] == "block"
                for item in result["failures"]
            )

        blocks = [
            failure_item
            for result in results
            for failure_item in result["failures"]
            if failure_item["severity"] == "block"
        ]

        route = None
        if hasattr(policy, "approval_snapshot"):
            from app.services.business_rules.expense_adapter import resolve_route
            route = await resolve_route(db, policy.approval_snapshot, user, str(total))

        return {
            "approval_route": route,
            "rule_type": "reimbursement",
            "rule_version": policy.revision,
            "schema_version": 1 if route else None,
            "approval_level": route["nodes"][0]["stage"] if route else "direct_manager",
            "rule_versions": ({"reimbursement": policy.revision, "approval": policy.approval_snapshot["version"]} if route else {}),
            "invoice_ids": sorted(body.invoice_ids),
            "trip_id": body.trip_id,
            "revision": policy.revision,
            "items": items,
            "results": results,
            "total_amount": str(total),
            "breakdown": {
                key: str(value) for key, value in breakdown.items()
            },
            "all_passed": not blocks,
            "can_override": bool(blocks) and all(
                item["allow_override"] for item in blocks
            ),
            # 提交时比较校验上下文，防止确认过程中数据发生变化。
            "validation_hash": digest({
                "approval_route": route,
                "revision": policy.revision,
                "facts": facts_list,
                "results": results,
            }),
        }

    async def check(self,user_id,body: ExpenseCheckInput):
        """
        报销单审核, 调用_evaluate
        :param user_id:
        :param body:
        :return:
        """
        async with create_session() as db,db.begin():
            policy = await locked_policy(db)
            user = await locked_user(db,user_id)
            return await self._evaluate(db, user,policy,body)

    async def prepare(self, user_id, body: ExpensePrepareInput):
        """
        报销单准备
        :param user_id:
        :param body:
        :return:
        """
        source_key = digest({
            "user_id": user_id,
            "trip_id": body.trip_id,
            "invoice_ids": sorted(body.invoice_ids),
        })

        try:
            async with create_session() as db, db.begin():
                policy = await locked_policy(db)
                user = await locked_user(db, user_id)

                existing = await db.scalar(
                    select(Expense)
                    .where(Expense.source_key == source_key)
                    .with_for_update()
                )
                if existing:
                    return detail_data(existing)

                report = await self._evaluate(db, user, policy, body)
                override = not report["all_passed"]

                if override and not (
                        report["can_override"] and body.accept_overrides
                ):
                    return {"status": "validation_failed", **report}

                department = await db.get(
                    Department, user.department_id,
                )
                approver_id = (
                    report["approval_route"]["nodes"][0]["user_id"] if report.get("approval_route")
                    else department.manager_user_id if department else None
                )
                approver = (
                    await db.get(User, approver_id)
                    if approver_id else None
                )
                if (
                        approver is None
                        or approver.status not in ACTIVE_STATUSES
                        or approver.user_id == user_id
                ):
                    raise ExpenseError("未配置有效的直属主管")

                row = Expense(
                    user_id=user_id,
                    submitter_id=user_id,
                    trip_id=body.trip_id,
                    total_amount=Decimal(report["total_amount"]),
                    status="draft",
                    expense_no="EXP-" + uuid4().hex.upper(),
                    source_key=source_key,
                    payload=report,
                    has_override=override,
                    override_reason=body.override_reason if override else None,
                    approver_id=approver_id,
                    submit_attempts=0,
                    notified=False,
                )
                db.add(row)
                await db.flush()

                for item in report["items"]:
                    detail = ExpenseItem(
                        expense_id=row.id,
                        category=item["category"],
                        amount=Decimal(item["amount"]),
                        invoice_no=item["invoice_number"],
                        invoice_date=date.fromisoformat(item["invoice_date"]),
                        description=item["description"],
                    )
                    db.add(detail)
                    await db.flush()

                    invoice = await db.get(Invoice, item["invoice_id"])
                    invoice.expense_item_id = detail.id

                    db.add(ExpenseInvoiceClaim(
                        invoice_key=item["invoice_key"],
                        expense_id=row.id,
                    ))

                await db.flush()
                result = detail_data(row)

            return result
        except IntegrityError:
            raise ExpenseError(
                "发票已被其他报销单占用，请查询已有报销单",
                409,
            ) from None

    async def submit(self, user_id, expense_id):
        """
        报销单提交
        :param user_id:
        :param expense_id:
        :return:
        """
        async with create_session() as db, db.begin():
            policy = await locked_policy(db)
            user = await locked_user(db, user_id)
            row = await owned_expense(
                db, user_id, expense_id, lock=True,
            )

            if row.status in (
                    {"submitting", "approved", "paid"} | POLL_STATUSES
            ):
                return detail_data(row)
            if row.status != "draft" or not row.payload:
                raise ExpenseError("当前报销单不能提交")

            body = ExpenseCheckInput(
                trip_id=row.trip_id,
                invoice_ids=row.payload["invoice_ids"],
            )
            report = await self._evaluate(
                db, user, policy, body,
                current_expense_id=row.id,
            )

            if report["validation_hash"] != row.payload["validation_hash"]:
                raise ExpenseError(
                    "规则或校验数据已变化，请取消草稿后重新生成并确认",
                )

            if not report["all_passed"] and not (
                    row.has_override and report["can_override"]
            ):
                raise ExpenseError("报销校验未通过")

            approver = await db.get(User, row.approver_id)
            if (
                    approver is None
                    or approver.status not in ACTIVE_STATUSES
                    or approver.user_id == user_id
            ):
                raise ExpenseError(
                    "直属主管已失效，请取消草稿后重新生成",
                )

            # 此事务只记录员工确认，不调用外部系统。
            row.status = "submitting"
            row.next_attempt_at = utc_now()
            row.last_error = None
            return detail_data(row)

    async def cancel(self, user_id, expense_id):
        async with create_session() as db, db.begin():
            await locked_policy(db)
            await locked_user(db, user_id)
            row = await owned_expense(
                db, user_id, expense_id, lock=True,
            )
            if row.status == "cancelled":
                return detail_data(row)
            if row.status != "draft":
                raise ExpenseError("只允许取消尚未确认提交的草稿")

            invoice_ids = (row.payload or {}).get("invoice_ids", [])
            invoices = (
                await db.scalars(
                    select(Invoice)
                    .where(Invoice.id.in_(invoice_ids))
                    .order_by(Invoice.id)
                    .with_for_update()
                )
            ).all()
            for invoice in invoices:
                invoice.expense_item_id = None

            await db.execute(
                delete(ExpenseInvoiceClaim).where(
                    ExpenseInvoiceClaim.expense_id == row.id,
                )
            )
            row.status = "cancelled"
            row.source_key = None
            return detail_data(row)

    async def detail(self, user_id, expense_id):
        async with create_session() as db:
            row = await owned_expense(db, user_id, expense_id)
            return detail_data(row)


    async def trips(self, user_id):
        async with create_session() as db:
            rows = (
                await db.scalars(
                    select(BusinessTrip)
                    .where(
                        BusinessTrip.user_id == user_id,
                        BusinessTrip.status.in_(["approved", "completed"]),
                    )
                    .order_by(BusinessTrip.start_date.desc())
                    .limit(20)
                )
            ).all()
            return [{
                "trip_id": row.id,
                "destination": row.destination,
                "start_date": row.start_date.isoformat(),
                "end_date": row.end_date.isoformat(),
                "status": row.status,
            } for row in rows]

    async def list_rules(self, user_id):
        from app.services.business_rules.expense_adapter import legacy_principal
        from app.services.business_rules.service import RuleService
        snapshot = await RuleService().get(await legacy_principal(user_id), "reimbursement")
        return snapshot["rule_data"]["rules"]

    async def save_rule(self, user_id, rule_id, body: ExpenseRuleInput):
        from app.services.business_rules.expense_adapter import legacy_update
        return await legacy_update(user_id, rule_id=rule_id, rule=body)

    async def get_policy(self, user_id):
        from app.services.business_rules.expense_adapter import legacy_principal
        from app.services.business_rules.service import RuleService
        snapshot = await RuleService().get(await legacy_principal(user_id), "reimbursement")
        return {"revision": snapshot["version"], **snapshot["rule_data"]["policy"]}

    async def save_policy(self, user_id, body: ExpensePolicyInput):
        from app.services.business_rules.expense_adapter import legacy_update
        return await legacy_update(user_id, policy=body)

    async def reopen_rejected(self, user_id, expense_id):
        async with create_session() as db, db.begin():
            # 与 prepare/submit/cancel 保持一致的锁顺序。
            await locked_policy(db)
            await locked_user(db, user_id)
            row = await owned_expense(
                db, user_id, expense_id, lock=True,
            )

            if row.status not in REJECTED_STATUSES:
                raise ExpenseError("只有已退回的报销单可以重新办理")

            invoice_ids = (row.payload or {}).get("invoice_ids", [])

            if row.correction_opened_at is None:
                item_ids = list((await db.scalars(
                    select(ExpenseItem.id)
                    .where(ExpenseItem.expense_id == row.id)
                )).all())

                invoices = list((await db.scalars(
                    select(Invoice)
                    .where(Invoice.expense_item_id.in_(item_ids))
                    .order_by(Invoice.id)
                    .with_for_update()
                )).all())

                for invoice in invoices:
                    invoice.expense_item_id = None

                # 这里只释放业务占用，不删除原单、明细和审批日志。
                await db.execute(
                    delete(ExpenseInvoiceClaim).where(
                        ExpenseInvoiceClaim.expense_id == row.id,
                    )
                )

                row.source_key = None
                row.correction_opened_at = utc_now()

            return {
                "expense_id": row.id,
                "invoice_ids": invoice_ids,
                "trip_id": row.trip_id,
                "message": (
                    "原报销单和审批记录已保留，发票占用已释放。"
                    "补充费用信息后，请重新生成并确认报销单。"
                    "如果需要更换发票，请先发送“我要报销”，"
                    "再上传更正后的发票。"
                ),
            }
