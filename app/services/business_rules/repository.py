import asyncio
import json
import logging
import time
from contextlib import suppress

from sqlalchemy import select

from app.config.settings import get_settings
from app.core import redis_client as redis_module
from app.core.database import create_session
from app.models.business_rule import BusinessRule
from app.services.business_rules.common import RuleError, unpack, digest, RULE_TYPES
from app.services.business_rules.validation import validate_data

log = logging.getLogger(__name__)


def prefix():
    return f"dep:{get_settings().rules_environment}:rules"


class RuleRepository:
    def __init__(self):
        self.cache = {}
        self.checked = 0.0
        self.tasks = []
        self.lock = asyncio.Lock()

    async def reconcile(self):
        async with self.lock:
            async with create_session() as db:
                rows = (
                    await db.execute(
                        select(
                            BusinessRule.rule_type,
                            BusinessRule.version,
                            BusinessRule.content_hash,
                        )
                    )
                ).all()
            fresh = {}
            for row in rows:
                previous = self.cache.get(row.rule_type)
                if previous and previous[0] > row.version:
                    raise RuleError("规则数据库版本发生倒退", 503, "RULE_CORRUPT")
                if previous and previous[0] == row.version:
                    fresh[row.rule_type] = previous
                    continue
                envelope = None
                client = redis_module.redis_client
                if client is not None:
                    try:
                        async with asyncio.timeout(0.1):
                            cached = await client.get(
                                f"{prefix()}:{row.rule_type}:v:{row.version}"
                            )
                        if cached:
                            candidate = json.loads(cached)
                            value = unpack(candidate, row.rule_type)
                            content = {
                                k: value[k]
                                for k in (
                                    "rule_data",
                                    "bindings",
                                    "schema_version",
                                    "executor_version",
                                )
                            }
                            if (
                                value["version"] == row.version
                                and digest(content) == row.content_hash
                            ):
                                envelope = candidate
                    except Exception:
                        pass
                if envelope is None:
                    from app.models.business_rule import RuleVersionHistory

                    async with create_session() as db:
                        history = await db.get(
                            RuleVersionHistory, (row.rule_type, row.version)
                        )
                        if history is None:
                            raise RuleError("规则历史快照缺失", 503, "RULE_CORRUPT")
                        envelope = history.payload
                snapshot = unpack(envelope, row.rule_type)
                content = {
                    k: snapshot[k]
                    for k in (
                        "rule_data",
                        "bindings",
                        "schema_version",
                        "executor_version",
                    )
                }
                if (
                    snapshot["version"] != row.version
                    or snapshot["content_hash"] != row.content_hash
                    or digest(content) != row.content_hash
                ):
                    raise RuleError("规则缓存校验失败", 503, "RULE_CORRUPT")
                if snapshot["schema_version"] != 1 or snapshot["executor_version"] != 1:
                    raise RuleError("规则版本不兼容", 503, "RULE_VERSION_UNSUPPORTED")
                validate_data(row.rule_type, snapshot["rule_data"])
                fresh[row.rule_type] = (row.version, envelope)
            self.cache = fresh
            self.checked = time.monotonic()

    async def get(self, rule_type):
        if time.monotonic() - self.checked >= 5:
            try:
                async with asyncio.timeout(1):
                    await self.reconcile()
            except Exception:
                if time.monotonic() - self.checked >= 10:
                    raise RuleError(
                        "无法确认最新规则，请稍后重试", 503, "RULE_UNAVAILABLE"
                    ) from None
        cached = self.cache.get(rule_type)
        if not cached:
            raise RuleError("规则尚未发布", 503, "RULE_NOT_CONFIGURED")
        return unpack(cached[1], rule_type)

    async def poll(self):
        while True:
            try:
                async with asyncio.timeout(3):
                    await self.reconcile()
            except Exception as exc:
                log.warning("rules_reconcile_failed error_type=%s", type(exc).__name__)
            await asyncio.sleep(5)

    async def listen(self):
        while True:
            try:
                client = redis_module.redis_client
                if client is None:
                    raise RuntimeError("Redis unavailable")
                async with client.pubsub() as subscriber:
                    await subscriber.subscribe(prefix() + ":updated")
                    await self.reconcile()
                    async for message in subscriber.listen():
                        if message["type"] == "message":
                            await self.reconcile()
            except Exception as exc:
                log.warning(
                    "rules_subscription_failed error_type=%s", type(exc).__name__
                )
                await asyncio.sleep(1)

    async def start(self):
        if self.tasks:
            return
        await self.reconcile()
        if get_settings().business_rules_enabled and any(
            t not in self.cache for t in RULE_TYPES
        ):
            raise RuleError(
                "启用业务切换前必须发布三类首版规则", 503, "RULE_NOT_CONFIGURED"
            )
        self.tasks = [
            asyncio.create_task(self.poll()),
            asyncio.create_task(self.listen()),
        ]

    async def close(self):
        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            with suppress(asyncio.CancelledError):
                await task
        self.tasks = []
        self.cache, self.checked = {}, 0.0


repository = RuleRepository()


async def cache_version(rule_type, version):
    from app.models.business_rule import RuleVersionHistory

    async with create_session() as db:
        row = await db.get(RuleVersionHistory, (rule_type, version))
        if not row:
            raise RuleError("缓存事件引用不存在的历史版本", 503)
        payload = json.dumps(row.payload, ensure_ascii=False)
    client = redis_module.redis_client
    if client is None:
        raise RuntimeError("Redis unavailable")
    key = prefix() + ":" + rule_type
    await client.set(f"{key}:v:{version}", payload, ex=86400)
    await client.eval(
        """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
local incoming = tonumber(ARGV[1])
if incoming > current then redis.call('SET', KEYS[1], ARGV[1]) end
redis.call('PUBLISH', ARGV[2], ARGV[3])
return 1
""",
        1,
        key + ":current",
        version,
        prefix() + ":updated",
        json.dumps({"rule_type": rule_type, "version": version}),
    )
