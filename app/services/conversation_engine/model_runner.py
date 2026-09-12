"""外层超时后，直到工作线程实际结束才归还并发名额。"""
import asyncio
from app.core.rag_context import phase


class ModelRunner:
    def __init__(self, concurrency=5, queue_timeout=1.0, timeout=10.0):
        self.slots = asyncio.Semaphore(concurrency)
        self.queue_timeout = queue_timeout
        self.timeout = timeout
        self.tasks = set()

    async def run(self, function, on_timeout=None):
        with phase("model_queue"):
            await asyncio.wait_for(self.slots.acquire(), self.queue_timeout)
        try:
            task = asyncio.create_task(asyncio.to_thread(function))
        except BaseException:
            self.slots.release()
            raise
        self.tasks.add(task)
        def completed(done):
            self.tasks.discard(done)
            self.slots.release()
            if not done.cancelled():
                done.exception()  # 消费超时调用在后台产生的异常。
        task.add_done_callback(completed)
        with phase("model_execution"):
            try:
                return await asyncio.wait_for(asyncio.shield(task), self.timeout)
            except (TimeoutError, asyncio.CancelledError):
                if on_timeout is not None:
                    on_timeout()
                raise

    async def close(self):
        if self.tasks:
            await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
