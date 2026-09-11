"""请求级截止时间与无正文日志；同一上下文可传入并发子任务。"""
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from dataclasses import dataclass
from uuid import uuid4


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class QueryBudget:
    request_id: str
    deadline_unix: float
    deadline_monotonic: float

    def remaining(self) -> float:
        seconds = self.deadline_monotonic - time.monotonic()
        if seconds <= 0:
            raise TimeoutError("知识查询预算耗尽")
        return seconds


_budget: ContextVar[QueryBudget | None] = ContextVar("rag_budget", default=None)
_trace: ContextVar[str | None] = ContextVar("rag_trace", default=None)


def current_budget() -> QueryBudget | None:
    return _budget.get()


def remaining_seconds(default: float) -> float:
    budget = current_budget()
    return min(default, budget.remaining()) if budget else default


@contextmanager
def query_scope(timeout: float = 5.0, *, request_id=None, deadline_unix=None):
    now = time.time()
    deadline = min(deadline_unix, now + timeout) if deadline_unix is not None else now + timeout
    budget = QueryBudget(request_id or _trace.get() or uuid4().hex, deadline,
                         time.monotonic() + max(0.0, deadline - now))
    token = _budget.set(budget)
    try:
        yield budget
    finally:
        _budget.reset(token)


@contextmanager
def phase(name: str, **fields):
    started = time.perf_counter()
    outcome = "ok"
    try:
        yield
    except BaseException:
        outcome = "error"
        raise
    finally:
        budget = current_budget()
        log.info("rag_stage request_id=%s stage=%s elapsed_ms=%.2f outcome=%s metrics=%s",
                 budget.request_id if budget else (_trace.get() or "-"), name,
                 (time.perf_counter() - started) * 1000, outcome, fields)


def timed(name):
    """用于飞书边界计时，不改变被包装函数的调用参数。"""
    def decorate(function):
        @wraps(function)
        async def wrapped(*args, **kwargs):
            with phase(name):
                return await function(*args, **kwargs)
        return wrapped
    return decorate


def traced(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        token = _trace.set(uuid4().hex)
        try:
            return await function(*args, **kwargs)
        finally:
            _trace.reset(token)
    return wrapped
