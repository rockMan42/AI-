import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager, suppress

from app.config.settings import get_settings
from app.core import redis_client as redis_module


log = logging.getLogger(__name__)

QUEUE_KEY = "dep:cron:queue"
TASK_PREFIX = "dep:cron:task:"
SUPPORTED_TYPES = {
    "HOLIDAY_NOTICE_PUSH",
    "RECEIPT_REMINDER",
    "APPROVAL_POLL",
    "EXPENSE_APPROVAL_POLL",
    "EXPENSE_TIMEOUT_SCAN",
    "EXPENSE_NOTIFICATION_DELIVERY",
}

_worker = None
_stop = None


class HolidayError(ValueError):
    def __init__(
        self,
        message: str,
        status_code: int = 409,
        *,
        permanent: bool = False,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.permanent = permanent


def cache():
    client = redis_module.redis_client
    if client is None:
        raise HolidayError("Redis 尚未初始化", 503)
    return client


def now_ms() -> int:
    return int(time.time() * 1000)


def task_key(task_id: str) -> str:
    return TASK_PREFIX + task_id


def push_task_id(notice_id: int) -> str:
    return f"HOLIDAY_NOTICE_PUSH:{notice_id}"


@asynccontextmanager
async def lease(key: str):
    lock = cache().lock(
        key,
        timeout=60,
        blocking=False,
        thread_local=False,
    )
    if not await lock.acquire():
        raise HolidayError("操作正在处理中，请稍后重试")

    stop = asyncio.Event()

    async def renew():
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=15)
            except TimeoutError:
                # 续期失败会取消 TaskGroup 中的业务执行。
                await lock.extend(60, replace_ttl=True)

    try:
        try:
            async with asyncio.TaskGroup() as group:
                group.create_task(renew())
                try:
                    yield
                finally:
                    stop.set()
        except* HolidayError as exc_group:
            # TaskGroup 会将 yield 中的业务异常包装为 ExceptionGroup，
            # 这里解包后交给接口层按原有的状态码处理。
            raise exc_group.exceptions[0]
    finally:
        # redis-py 使用锁 token 校验，不会删除其他实例的锁。
        with suppress(Exception):
            await lock.release()


def notice_lease(notice_id: int):
    # 修改、推送、催办和报告统一按通知串行。
    return lease(
        f"dep:cron:lock:{push_task_id(notice_id)}"
    )


REGISTER_SCRIPT = """
if redis.call('EXISTS', KEYS[1]) == 1 and ARGV[4] ~= '1' then
    return 0
end
if ARGV[4] == '1' then
    redis.call('DEL', KEYS[1])
end
local values = cjson.decode(ARGV[3])
for field, value in pairs(values) do
    redis.call('HSET', KEYS[1], field, value)
end
redis.call('ZADD', KEYS[2], ARGV[2], ARGV[1])
return 1
"""


async def register_task(
    task_id: str,
    task_type: str,
    payload: dict,
    execute_at: int,
    *,
    max_retries: int = 3,
    replace: bool = False,
):
    if task_type not in SUPPORTED_TYPES:
        raise HolidayError("不支持的任务类型", 422)

    detail = {
        "task_id": task_id,
        "task_type": task_type,
        "payload": json.dumps(payload, ensure_ascii=False),
        "status": "PENDING",
        "execute_at": str(execute_at),
        "attempts": "0",
        "retry_count": "0",
        "max_retries": str(max_retries),
        "created_at": str(now_ms()),
    }

    await cache().eval(
        REGISTER_SCRIPT,
        2,
        task_key(task_id),
        QUEUE_KEY,
        task_id,
        execute_at,
        json.dumps(detail),
        "1" if replace else "0",
    )
    return task_id


async def read_task(task_id: str) -> dict:
    data = await cache().hgetall(task_key(task_id))
    if data:
        data["payload"] = json.loads(data["payload"])
    return data


async def finish_task(
    task_id: str,
    status: str = "COMPLETED",
):
    async with cache().pipeline(transaction=True) as pipe:
        pipe.hset(
            task_key(task_id),
            mapping={
                "status": status,
                "finished_at": str(now_ms()),
            },
        )
        pipe.zrem(QUEUE_KEY, task_id)
        await pipe.execute()


async def retry_task(task: dict, exc: Exception):
    attempts = int(task["attempts"])
    maximum = int(task["max_retries"])
    permanent = (
        isinstance(exc, HolidayError) and exc.permanent
    )

    failed = permanent or attempts >= maximum + 1

    async with cache().pipeline(transaction=True) as pipe:
        pipe.hset(
            task_key(task["task_id"]),
            mapping={
                "status": "FAILED" if failed else "PENDING",
                "last_error": type(exc).__name__,
                "retry_count": str(min(attempts, maximum)),
            },
        )
        if failed:
            pipe.zrem(QUEUE_KEY, task["task_id"])
        else:
            retry_at = now_ms() + 5 * 60 * 1000
            pipe.hset(
                task_key(task["task_id"]),
                "execute_at",
                retry_at,
            )
            pipe.zadd(
                QUEUE_KEY,
                {task["task_id"]: retry_at},
            )
        await pipe.execute()


async def execute_task(task_id: str, *, force: bool = False):
    # 延迟导入，避免业务模块和调度模块循环导入。
    from app.services.holiday import (
        run_push,
        run_receipt_task,
        schedule_followups,
    )

    task = await read_task(task_id)
    if not task:
        await cache().zrem(QUEUE_KEY, task_id)
        return {"status": "MISSING"}

    if task["task_type"] not in SUPPORTED_TYPES:
        return {"status": "IGNORED"}

    if task["task_type"] == "APPROVAL_POLL":
        from app.services.requisition.approval_poll_service import (
            execute_approval_poll,
        )
        return await execute_approval_poll(
            task_id,
            force=force,
        )

    if task["task_type"].startswith("EXPENSE_"):
        from app.services.expense.approval_cron import (
            execute_expense_task,
        )
        return await execute_expense_task(
            task_id,
            force=force,
        )

    notice_id = int(task["payload"]["notice_id"])

    async with notice_lease(notice_id):
        task = await read_task(task_id)

        if task["status"] in {
            "COMPLETED", "FAILED", "CANCELLED",
        }:
            await cache().zrem(QUEUE_KEY, task_id)
            return {"status": task["status"]}

        if not force and int(task["execute_at"]) > now_ms():
            return {"status": "PENDING"}

        attempts = int(task["attempts"])
        if attempts >= int(task["max_retries"]) + 1:
            await finish_task(task_id, "FAILED")
            return {"status": "FAILED"}

        task["attempts"] = str(attempts + 1)
        await cache().hset(
            task_key(task_id),
            mapping={
                "status": "EXECUTING",
                "attempts": task["attempts"],
            },
        )

        try:
            if task["task_type"] == "HOLIDAY_NOTICE_PUSH":
                await run_push(task)
            else:
                await run_receipt_task(task)
        except Exception as exc:
            log.error(
                "holiday_task_failed task_id=%s error_type=%s",
                task_id,
                type(exc).__name__,
            )
            await retry_task(task, exc)
            result = await read_task(task_id)
            return {"status": result["status"]}

        await finish_task(task_id)

        if task["task_type"] == "HOLIDAY_NOTICE_PUSH":
            try:
                await schedule_followups(notice_id)
            except Exception as exc:
                # 推送已完成；后续注册由补偿检查修复。
                log.error(
                    "holiday_followup_registration_failed "
                    "notice_id=%s error_type=%s",
                    notice_id,
                    type(exc).__name__,
                )

        return {"status": "COMPLETED"}


async def _worker_loop():
    from app.services.holiday import reconcile_published

    next_reconcile = 0.0
    while not _stop.is_set():
        try:
            # 当前队列只新增上述两种任务。
            # 不消费未来任务，也不提前弹出正在执行的任务。
            task_ids = await cache().zrangebyscore(
                QUEUE_KEY,
                "-inf",
                now_ms(),
            )

            for task_id in task_ids:
                if _stop.is_set():
                    break
                try:
                    await execute_task(task_id)
                except HolidayError:
                    # 其他实例或手动接口持有通知锁。
                    continue
                except Exception as exc:
                    log.error(
                        "holiday_dispatch_failed task_id=%s "
                        "error_type=%s",
                        task_id,
                        type(exc).__name__,
                    )

            if time.monotonic() >= next_reconcile:
                from app.services.expense.approval_cron import (
                    reconcile_tasks,
                )

                # 某一业务恢复失败，不阻断其他业务的恢复。
                for reconcile in (reconcile_published, reconcile_tasks):
                    try:
                        await reconcile()
                    except Exception as exc:
                        log.error(
                            "cron_reconcile_failed "
                            "handler=%s error_type=%s",
                            reconcile.__name__,
                            type(exc).__name__,
                        )

                next_reconcile = time.monotonic() + 30
        except Exception as exc:
            log.error(
                "holiday_worker_failed error_type=%s",
                type(exc).__name__,
            )

        try:
            await asyncio.wait_for(
                _stop.wait(),
                timeout=get_settings().holiday_worker_poll_seconds,
            )
        except TimeoutError:
            pass


async def start_holiday_worker():
    global _worker, _stop

    if _worker is not None:
        return
    _stop = asyncio.Event()
    _worker = asyncio.create_task(
        _worker_loop(),
        name="holiday-notifications",
    )


async def stop_holiday_worker():
    global _worker, _stop

    if _worker is None:
        return

    _stop.set()
    await _worker
    _worker = None
    _stop = None
