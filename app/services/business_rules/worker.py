import asyncio
import logging
from contextlib import suppress
from datetime import timedelta
from uuid import uuid4, uuid5, NAMESPACE_URL

from sqlalchemy import or_, select

from app.config.settings import get_settings
from app.core.database import create_session
from app.models.business_rule import (
    BusinessRule,
    RuleOutbox,
    RuleDocBinding,
    RuleVersionHistory,
)
from app.services.business_rules.common import digest, unpack
from app.services.business_rules.repository import repository, cache_version
from app.services.business_rules.feishu import document_meta, plain_card, sync_bitable
from app.services.business_rules.presentation import label
from app.services.notification.legacy import send_feishu_card
from app.utils.time import utc_now

log = logging.getLogger(__name__)
_tasks = []


async def deliver_one():
    now, token = utc_now(), uuid4().hex
    settings = get_settings()
    async with create_session() as db, db.begin():
        query = select(RuleOutbox).where(
            RuleOutbox.status != "done",
            RuleOutbox.next_attempt_at <= now,
            or_(RuleOutbox.lease_until.is_(None), RuleOutbox.lease_until < now),
        )
        if not settings.rules_feishu_enabled:
            query = query.where(RuleOutbox.kind == "cache")
        row = await db.scalar(
            query.order_by(RuleOutbox.next_attempt_at, RuleOutbox.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if row is None:
            return False
        row.lease_token, row.lease_until = token, now + timedelta(seconds=60)
        row.status, row.attempts = "processing", row.attempts + 1
        identifier, kind, payload = row.id, row.kind, dict(row.payload)
    error, remote_id = None, None
    try:
        async with asyncio.timeout(40):
            if kind == "cache":
                await cache_version(payload["rule_type"], payload["version"])
            elif kind == "bitable":
                async with create_session() as db:
                    version = await db.get(
                        RuleVersionHistory, (payload["rule_type"], payload["version"])
                    )
                    snapshot = unpack(version.payload, payload["rule_type"])
                    bindings = (
                        await db.scalars(
                            select(RuleDocBinding).where(
                                RuleDocBinding.rule_type == payload["rule_type"]
                            )
                        )
                    ).all()
                    snapshot["document_status_text"] = (
                        "监控异常"
                        if any(b.error for b in bindings)
                        else "待 HR 确认"
                        if any(
                            b.observed_revision > b.acknowledged_revision
                            for b in bindings
                        )
                        else "已确认"
                        if bindings
                        else "未绑定"
                    )
                remote_id = await sync_bitable(snapshot, identifier)
            elif kind == "document_reminder":
                card = plain_card(
                    "制度变更待确认",
                    f"规则：{label(payload['rule_type'])}\n文档：{payload['doc_id']}\n新修订号：{payload['revision']}\n请 HR 核对并修改规则，或说明无需修改后确认新制度版本。",
                )
                remote_id = await send_feishu_card(
                    payload["recipient"], card, str(uuid5(NAMESPACE_URL, identifier))
                )
            else:
                raise ValueError("Unsupported rule outbox event")
    except Exception as exc:
        error = type(exc).__name__
    async with create_session() as db, db.begin():
        row = await db.scalar(
            select(RuleOutbox).where(RuleOutbox.id == identifier).with_for_update()
        )
        if row.lease_token != token:
            return True
        row.lease_until, row.lease_token = None, None
        row.last_error = error
        row.status = "pending" if error else "done"
        row.remote_id = remote_id or row.remote_id
        row.next_attempt_at = utc_now() + timedelta(
            seconds=min(300, 2 ** min(row.attempts, 8))
        )
        if error:
            log.warning(
                "rule_outbox_retry kind=%s attempts=%s error_type=%s",
                kind,
                row.attempts,
                error,
            )
        age = (utc_now() - row.created_at).total_seconds()
        if (kind == "cache" and age > 10) or (
            kind == "document_reminder" and age > 6000
        ):
            log.error("rule_delivery_sla_exceeded kind=%s age_seconds=%.1f", kind, age)
    return True


async def check_documents():
    settings = get_settings()
    if not settings.rules_feishu_enabled:
        return
    if not settings.rules_hr_open_ids:
        log.error("rules_document_recipients_missing")
        return
    async with create_session() as db:
        bindings = list(
            (
                await db.execute(
                    select(RuleDocBinding.rule_type, RuleDocBinding.doc_id)
                )
            ).all()
        )
    for rule_type, doc_id in bindings:
        try:
            meta = await document_meta(doc_id)
            async with create_session() as db, db.begin():
                row = await db.scalar(
                    select(RuleDocBinding)
                    .where(
                        RuleDocBinding.rule_type == rule_type,
                        RuleDocBinding.doc_id == doc_id,
                    )
                    .with_for_update()
                )
                if row is None:
                    continue
                row.checked_at, row.error = utc_now(), None
                revision = max(row.observed_revision, meta["revision_id"])
                row.observed_revision = revision
                if revision <= row.acknowledged_revision:
                    continue
                for recipient in set(settings.rules_hr_open_ids):
                    identifier = "doc:" + digest(
                        [rule_type, doc_id, revision, recipient]
                    )
                    if not await db.get(RuleOutbox, identifier):
                        db.add(
                            RuleOutbox(
                                id=identifier,
                                kind="document_reminder",
                                payload={
                                    "rule_type": rule_type,
                                    "doc_id": doc_id,
                                    "revision": revision,
                                    "recipient": recipient,
                                },
                                next_attempt_at=utc_now(),
                            )
                        )
                current = await db.get(BusinessRule, rule_type)
                sync_id = "bitable-doc:" + digest(
                    [rule_type, doc_id, revision, current.version]
                )
                if not await db.get(RuleOutbox, sync_id):
                    db.add(
                        RuleOutbox(
                            id=sync_id,
                            kind="bitable",
                            payload={
                                "rule_type": rule_type,
                                "version": current.version,
                            },
                            next_attempt_at=utc_now(),
                        )
                    )
        except Exception as exc:
            async with create_session() as db, db.begin():
                row = await db.get(RuleDocBinding, (rule_type, doc_id))
                if row:
                    row.error = type(exc).__name__
            log.warning("rule_document_check_failed error_type=%s", type(exc).__name__)


async def delivery_loop():
    while True:
        try:
            if await deliver_one():
                continue
        except Exception as exc:
            log.warning("rule_delivery_failed error_type=%s", type(exc).__name__)
        await asyncio.sleep(1)


async def document_loop():
    while True:
        try:
            await check_documents()
        except Exception as exc:
            log.warning("rule_document_worker_failed error_type=%s", type(exc).__name__)
        await asyncio.sleep(900)


async def start_rule_workers():
    settings = get_settings()
    if not (settings.business_rules_enabled or settings.rules_worker_enabled):
        return
    await repository.start()
    if settings.rules_worker_enabled and not _tasks:
        _tasks.extend(
            [asyncio.create_task(delivery_loop()), asyncio.create_task(document_loop())]
        )


async def stop_rule_workers():
    for task in _tasks:
        task.cancel()
    for task in _tasks:
        with suppress(asyncio.CancelledError):
            await task
    _tasks.clear()
    await repository.close()
