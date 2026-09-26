import asyncio
import hashlib
import secrets
import time
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError

from app.config.settings import get_settings
from app.core.database import create_session
from app.models.business_rule import (
    BusinessRule,
    RuleVersionHistory,
    RuleDocBinding,
    RuleChangeRequest,
    RuleOutbox,
)
from app.models.permission import OrganizationState
from app.models.user import User
from app.schemas.business_rule import RuleUpdate
from app.schemas.permission import Role
from app.security.auth import ACTIVE_STATUSES
from app.security.permission import authorize
from app.services.permission_audit import append_audit
from app.services.business_rules.common import (
    RULE_TYPES,
    RuleError,
    digest,
    diff,
    pack,
    unpack,
)
from app.services.business_rules.validation import schema_for, validate_data
from app.services.business_rules.evaluators import evaluate_snapshot
from app.utils.time import utc_now


async def guard(principal, action="rules.read"):
    # 拒绝操作单独记审计；成功发布审计只随业务事务提交。
    if principal.role != Role.HR_ADMIN or action == "rules.read":
        await authorize(principal, action)


async def lock_permission(db, principal):
    state = await db.scalar(
        select(OrganizationState).where(OrganizationState.id == 1).with_for_update()
    )
    if (
        not state
        or state.dirty
        or state.version != principal.version
        or time.time() - state.synced_at > get_settings().permission_org_max_age
    ):
        raise RuleError("权限已变化或正在同步，请重试", 503, "PERMISSION_UNAVAILABLE")
    actor = await db.get(User, principal.user_id)
    if (
        principal.role != Role.HR_ADMIN
        or not actor
        or actor.status not in ACTIVE_STATUSES
    ):
        raise RuleError("仅有效 HRAdmin 可管理规则", 403, "RULE_FORBIDDEN")


async def read_current(db, rule_type, *, lock=False):
    if rule_type not in RULE_TYPES:
        raise RuleError("规则类型不存在", 404, "RULE_NOT_FOUND")
    row = (
        await db.scalar(
            select(BusinessRule)
            .where(BusinessRule.rule_type == rule_type)
            .with_for_update(read=True)
        )
        if lock
        else await db.get(BusinessRule, rule_type)
    )
    if row is None:
        raise RuleError("规则尚未发布", 404, "RULE_NOT_CONFIGURED")
    snapshot = unpack(row.payload, rule_type)
    if snapshot["version"] != row.version:
        raise RuleError("规则快照版本异常", 503, "RULE_CORRUPT")
    return snapshot


def public_result(snapshot):
    return {
        "rule_type": snapshot["rule_type"],
        "version": snapshot["version"],
        "content_hash": snapshot["content_hash"],
        "propagation": "pending",
    }


class RuleService:
    async def difference(self, principal, rule_type, from_version, to_version):
        before = await self.get(principal, rule_type, from_version)
        after = await self.get(principal, rule_type, to_version)
        async with create_session() as db:
            rows = [
                await db.get(RuleVersionHistory, (rule_type, v))
                for v in (from_version, to_version)
            ]
        changes = diff(
            {"rule_data": before["rule_data"], "bindings": before["bindings"]},
            {"rule_data": after["rule_data"], "bindings": after["bindings"]},
        )
        if rule_type in get_settings().rules_sensitive_types or any(
            "ciphertext" in r.payload for r in rows
        ):
            changes = [
                {"path": c["path"], "before": "***", "after": "***"} for c in changes
            ]
        return {"from_version": from_version, "to_version": to_version, "diff": changes}

    async def health(self, principal):
        await guard(principal)
        from app.services.business_rules.repository import repository

        now = utc_now()
        async with create_session() as db:
            versions = dict(
                (
                    await db.execute(
                        select(BusinessRule.rule_type, BusinessRule.version)
                    )
                ).all()
            )
            pending = (
                await db.execute(
                    select(
                        RuleOutbox.kind, func.count(), func.min(RuleOutbox.created_at)
                    )
                    .where(RuleOutbox.status != "done")
                    .group_by(RuleOutbox.kind)
                )
            ).all()
            bindings = (await db.scalars(select(RuleDocBinding))).all()
        age = time.monotonic() - repository.checked if repository.checked else None
        return {
            "worker_versions": {k: v[0] for k, v in repository.cache.items()},
            "database_versions": versions,
            "last_verified_age_seconds": age,
            "fresh": age is not None and age < 10,
            "pending_events": [
                {
                    "kind": kind,
                    "count": count,
                    "oldest_age_seconds": max(0, (now - created).total_seconds()),
                }
                for kind, count, created in pending
            ],
            "documents": [
                {
                    "doc_id": b.doc_id,
                    "rule_type": b.rule_type,
                    "checked_at": b.checked_at,
                    "error": b.error,
                    "needs_confirmation": b.observed_revision > b.acknowledged_revision,
                }
                for b in bindings
            ],
        }

    async def list(self, principal):
        await guard(principal)
        async with create_session() as db:
            rows = (
                await db.scalars(select(BusinessRule).order_by(BusinessRule.rule_type))
            ).all()
            return [
                {
                    "rule_type": r.rule_type,
                    "version": r.version,
                    "updated_by": r.updated_by,
                    "updated_at": r.updated_at,
                }
                for r in rows
            ]

    async def get(self, principal, rule_type, version=None):
        await guard(principal)
        async with create_session() as db:
            if version is not None:
                row = await db.get(RuleVersionHistory, (rule_type, version))
                if not row:
                    raise RuleError("历史版本不存在", 404, "RULE_NOT_FOUND")
                return unpack(row.payload, rule_type)
            snapshot = await read_current(db, rule_type)
            bindings = (
                await db.scalars(
                    select(RuleDocBinding).where(RuleDocBinding.rule_type == rule_type)
                )
            ).all()
            return {
                **snapshot,
                "document_status": [
                    {
                        "doc_id": b.doc_id,
                        "acknowledged_revision": b.acknowledged_revision,
                        "observed_revision": b.observed_revision,
                        "checked_at": b.checked_at,
                        "error": b.error,
                    }
                    for b in bindings
                ],
            }

    async def versions(self, principal, rule_type, offset=0, limit=20):
        await guard(principal)
        async with create_session() as db:
            rows = (
                await db.scalars(
                    select(RuleVersionHistory)
                    .where(RuleVersionHistory.rule_type == rule_type)
                    .order_by(RuleVersionHistory.version.desc())
                    .offset(offset)
                    .limit(limit)
                )
            ).all()
            return [
                {
                    "version": r.version,
                    "changed_by": r.changed_by,
                    "change_summary": r.change_summary,
                    "created_at": r.created_at,
                    "rollback_from_version": r.rollback_from_version,
                }
                for r in rows
            ]

    async def preview(self, principal, rule_type, body):
        await guard(principal)
        data = validate_data(rule_type, body.rule_data)
        async with create_session() as db:
            current = await db.get(BusinessRule, rule_type)
            before = unpack(current.payload, rule_type) if current else None
        if (before["version"] if before else 0) != body.expected_version:
            raise RuleError("规则版本已变化", 409, "RULE_VERSION_CONFLICT")
        after = self.snapshot(rule_type, body.expected_version + 1, body, data)
        samples = []
        for facts in getattr(body, "samples", []):

            def calculate(snapshot):
                if not snapshot:
                    return None
                try:
                    return evaluate_snapshot(snapshot, facts)
                except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
                    return {"error": getattr(exc, "code", "SAMPLE_INVALID")}

            samples.append({"before": calculate(before), "after": calculate(after)})
        changes = diff(
            {"rule_data": before["rule_data"], "bindings": before["bindings"]}
            if before
            else {},
            {"rule_data": data, "bindings": after["bindings"]},
        )
        if (
            current and "ciphertext" in current.payload
        ) or rule_type in get_settings().rules_sensitive_types:
            changes = [
                {"path": c["path"], "before": "***", "after": "***"} for c in changes
            ]
            samples = [{"redacted": True} for _ in samples]
        return {
            "expected_version": body.expected_version,
            "diff": changes,
            "samples": samples,
        }

    @staticmethod
    def snapshot(rule_type, version, body, data):
        bindings = sorted(
            [b.model_dump() for b in body.bindings], key=lambda x: x["doc_id"]
        )
        if len({b["doc_id"] for b in bindings}) != len(bindings):
            raise RuleError("制度文档不能重复绑定")
        content = {
            "rule_data": data,
            "bindings": bindings,
            "schema_version": 1,
            "executor_version": 1,
        }
        return {
            **content,
            "rule_type": rule_type,
            "version": version,
            "rule_schema": schema_for(rule_type),
            "content_hash": digest(content),
        }

    @staticmethod
    async def document_revisions(bindings):
        from app.services.business_rules.feishu import document_meta

        semaphore = asyncio.Semaphore(5)

        async def read(binding):
            async with semaphore:
                meta = await document_meta(binding["doc_id"])
                return binding["doc_id"], meta["revision_id"]

        try:
            async with asyncio.timeout(20):
                return dict(await asyncio.gather(*(read(b) for b in bindings)))
        except TimeoutError:
            raise RuleError(
                "制度版本核对超时，请重试", 503, "DOCUMENT_UNAVAILABLE"
            ) from None

    async def validate_references(self, db, snapshot, revisions, *, rollback=False):
        if snapshot["rule_type"] == "approval":
            data = snapshot["rule_data"]
            for route in [
                *data["chains"],
                *([data["default_chain"]] if data["default_chain"] else []),
            ]:
                for node in route["nodes"]:
                    if node["actor"]["kind"] == "user":
                        user = await db.get(User, node["actor"]["user_id"])
                        if not user or user.status not in ACTIVE_STATUSES:
                            raise RuleError("审批人不存在或已停用")
        for binding in snapshot["bindings"]:
            saved = await db.get(
                RuleDocBinding, (snapshot["rule_type"], binding["doc_id"])
            )
            if (
                saved
                and saved.observed_revision > binding["acknowledged_revision"]
                and not rollback
            ):
                raise RuleError("制度已有更新版本，请重新核对", 409, "DOCUMENT_CHANGED")
            # 网络读取在事务之前完成；事务中还核对监控已观察到的版本。
            revision = revisions[binding["doc_id"]]
            if (
                not rollback and revision != binding["acknowledged_revision"]
            ) or revision < binding["acknowledged_revision"]:
                raise RuleError("制度修订号已变化，请重新核对", 409, "DOCUMENT_CHANGED")

    async def _publish(
        self, db, principal, rule_type, body, rollback_version=None, *, revisions=None
    ):
        current = await db.scalar(
            select(BusinessRule)
            .where(BusinessRule.rule_type == rule_type)
            .with_for_update()
        )
        version = current.version if current else 0
        if version != body.expected_version:
            raise RuleError("规则版本已变化，请重新预览", 409, "RULE_VERSION_CONFLICT")
        data = validate_data(rule_type, body.rule_data)
        snapshot = self.snapshot(rule_type, version + 1, body, data)
        await self.validate_references(
            db, snapshot, revisions or {}, rollback=rollback_version is not None
        )
        if (
            current
            and current.content_hash == snapshot["content_hash"]
            and rollback_version is None
        ):
            return {
                **public_result(unpack(current.payload, rule_type)),
                "unchanged": True,
            }
        snapshot.update(
            {
                "changed_by": principal.user_id,
                "published_at": utc_now().isoformat() + "Z",
            }
        )
        envelope = pack(
            snapshot,
            rule_type,
            sensitive=bool(current and "ciphertext" in current.payload),
        )
        if not current:
            current = BusinessRule(rule_type=rule_type)
            db.add(current)
        before_version = version
        current.version = snapshot["version"]
        current.payload = envelope
        current.content_hash = snapshot["content_hash"]
        current.updated_by = principal.user_id
        db.add(
            RuleVersionHistory(
                rule_type=rule_type,
                version=current.version,
                payload=envelope,
                content_hash=current.content_hash,
                changed_by=principal.user_id,
                change_summary="敏感规则变更"
                if "ciphertext" in envelope
                else body.change_summary,
                rollback_from_version=rollback_version,
            )
        )
        existing = {
            b.doc_id: b
            for b in (
                await db.scalars(
                    select(RuleDocBinding).where(RuleDocBinding.rule_type == rule_type)
                )
            ).all()
        }
        for item in snapshot["bindings"]:
            bound = existing.pop(item["doc_id"], None)
            if bound is None:
                bound = RuleDocBinding(rule_type=rule_type, doc_id=item["doc_id"])
                db.add(bound)
            bound.doc_title = item["doc_title"]
            bound.acknowledged_revision = item["acknowledged_revision"]
            bound.observed_revision = max(
                bound.observed_revision or 0,
                item["acknowledged_revision"],
                (revisions or {}).get(item["doc_id"], 0),
            )
            bound.checked_at, bound.error = utc_now(), None
        for bound in existing.values():
            await db.delete(bound)
        await append_audit(
            db,
            principal.user_id,
            "rules.rollback" if rollback_version else "rules.write",
            True,
            rule_type,
            {
                "before_version": before_version,
                "after_version": current.version,
                "content_hash": current.content_hash,
            },
        )
        for kind in ("cache", "bitable"):
            db.add(
                RuleOutbox(
                    id=f"{kind}:{rule_type}:{current.version}",
                    kind=kind,
                    payload={"rule_type": rule_type, "version": current.version},
                    next_attempt_at=utc_now(),
                )
            )
        return public_result(snapshot)

    async def publish(self, principal, rule_type, body, request_key):
        await guard(principal, "rules.write")
        request_hash = digest({"type": rule_type, "body": body.model_dump(mode="json")})
        async with create_session() as db:
            prior = await db.scalar(
                select(RuleChangeRequest).where(
                    RuleChangeRequest.actor_id == principal.user_id,
                    RuleChangeRequest.request_key == request_key,
                )
            )
        # 已处理请求重试不依赖飞书可用性，实际结果仍在事务内复核。
        revisions = (
            {}
            if prior
            else await self.document_revisions([b.model_dump() for b in body.bindings])
        )
        try:
            async with create_session() as db, db.begin():
                await lock_permission(db, principal)
                # 权限状态锁串行管理发布，同时防止权限变更穿透提交。
                existing = await db.scalar(
                    select(RuleChangeRequest)
                    .where(
                        RuleChangeRequest.actor_id == principal.user_id,
                        RuleChangeRequest.request_key == request_key,
                    )
                    .with_for_update()
                )
                if existing:
                    if existing.request_hash != request_hash or not existing.result:
                        raise RuleError(
                            "同一请求编号不能更换内容或操作",
                            409,
                            "IDEMPOTENCY_CONFLICT",
                        )
                    return existing.result
                result = await self._publish(
                    db, principal, rule_type, body, revisions=revisions
                )
                db.add(
                    RuleChangeRequest(
                        id=uuid4().hex,
                        actor_id=principal.user_id,
                        rule_type=rule_type,
                        request_key=request_key,
                        request_hash=request_hash,
                        payload={},
                        expires_at=utc_now(),
                        result=result,
                    )
                )
        except IntegrityError:
            raise RuleError(
                "并发发布冲突，请重新读取版本", 409, "RULE_VERSION_CONFLICT"
            ) from None
        await self.refresh_local()
        return result

    async def candidate(self, principal, rule_type, body, request_key):
        await guard(principal, "rules.write")
        if (body.update is None) == (body.rollback_version is None):
            raise RuleError("必须且只能指定修改内容或回滚版本")
        request_hash = digest({"type": rule_type, "body": body.model_dump(mode="json")})
        async with create_session() as db, db.begin():
            await lock_permission(db, principal)
            previous = await db.scalar(
                select(RuleChangeRequest).where(
                    RuleChangeRequest.actor_id == principal.user_id,
                    RuleChangeRequest.request_key == request_key,
                )
            )
            if previous:
                if previous.request_hash != request_hash:
                    raise RuleError(
                        "同一请求编号不能更换内容", 409, "IDEMPOTENCY_CONFLICT"
                    )
                value = unpack(previous.payload, rule_type)
                return {
                    "change_id": previous.id,
                    "token": value["token"],
                    "result": previous.result,
                    "expires_at": previous.expires_at,
                }
            current = await db.get(BusinessRule, rule_type)
            if (current.version if current else 0) != body.expected_version:
                raise RuleError("规则版本已变化", 409, "RULE_VERSION_CONFLICT")
            if body.rollback_version:
                old = await db.get(
                    RuleVersionHistory, (rule_type, body.rollback_version)
                )
                if not old:
                    raise RuleError("回滚目标不存在", 404, "RULE_NOT_FOUND")
                snapshot = unpack(old.payload, rule_type)
                if snapshot["executor_version"] != 1 or snapshot["schema_version"] != 1:
                    raise RuleError("回滚版本不兼容当前执行器", 409)
                update = RuleUpdate(
                    expected_version=body.expected_version,
                    rule_data=snapshot["rule_data"],
                    bindings=snapshot["bindings"],
                    change_summary=body.change_summary,
                )
            else:
                update = body.update
                if update.expected_version != body.expected_version:
                    raise RuleError("候选基准版本不一致")
            normalized = validate_data(rule_type, update.rule_data)
            update = update.model_copy(update={"rule_data": normalized})
            token, identifier = secrets.token_urlsafe(32), uuid4().hex
            expires_at = utc_now() + timedelta(minutes=15)
            payload = {
                "update": update.model_dump(mode="json"),
                "rollback_version": body.rollback_version,
                "token": token,
            }
            db.add(
                RuleChangeRequest(
                    id=identifier,
                    actor_id=principal.user_id,
                    rule_type=rule_type,
                    request_key=request_key,
                    request_hash=request_hash,
                    payload=pack(
                        payload,
                        rule_type,
                        sensitive=bool(current and "ciphertext" in current.payload),
                    ),
                    token_hash=hashlib.sha256(token.encode()).hexdigest(),
                    expires_at=expires_at,
                )
            )
        return {"change_id": identifier, "token": token, "expires_at": expires_at}

    async def confirm(self, principal, rule_type, confirmation, *, rollback_only=False):
        await guard(principal, "rules.write")
        async with create_session() as db:
            prior = await db.get(RuleChangeRequest, confirmation.change_id)
        if (
            not prior
            or prior.actor_id != principal.user_id
            or prior.rule_type != rule_type
            or not secrets.compare_digest(
                prior.token_hash or "",
                hashlib.sha256(confirmation.token.encode()).hexdigest(),
            )
        ):
            raise RuleError(
                "确认凭据无效或不属于当前操作者", 403, "CONFIRMATION_INVALID"
            )
        prepared = unpack(prior.payload, rule_type)
        revisions = (
            {}
            if prior.result or prior.expires_at <= utc_now()
            else await self.document_revisions(prepared["update"]["bindings"])
        )
        try:
            async with create_session() as db, db.begin():
                await lock_permission(db, principal)
                candidate = await db.scalar(
                    select(RuleChangeRequest)
                    .where(RuleChangeRequest.id == confirmation.change_id)
                    .with_for_update()
                )
                if (
                    not candidate
                    or candidate.actor_id != principal.user_id
                    or candidate.rule_type != rule_type
                    or not secrets.compare_digest(
                        candidate.token_hash or "",
                        hashlib.sha256(confirmation.token.encode()).hexdigest(),
                    )
                ):
                    raise RuleError(
                        "确认凭据无效或不属于当前操作者", 403, "CONFIRMATION_INVALID"
                    )
                payload = unpack(candidate.payload, rule_type)
                if rollback_only and not payload["rollback_version"]:
                    raise RuleError("不是回滚确认凭据", 409)
                if candidate.result:
                    return candidate.result
                if candidate.expires_at <= utc_now():
                    raise RuleError(
                        "确认已过期，请重新预览", 409, "CONFIRMATION_EXPIRED"
                    )
                result = await self._publish(
                    db,
                    principal,
                    rule_type,
                    RuleUpdate.model_validate(payload["update"]),
                    payload["rollback_version"],
                    revisions=revisions,
                )
                candidate.result = result
        except IntegrityError:
            raise RuleError("并发发布冲突", 409, "RULE_VERSION_CONFLICT") from None
        await self.refresh_local()
        return result

    @staticmethod
    async def refresh_local():
        from app.services.business_rules.repository import repository

        try:
            await repository.reconcile()
        except Exception:
            # 数据库提交已经成功，由 Outbox 和周期对账修复传播。
            pass
