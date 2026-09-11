"""知识 MCP 专用连接：SDK 会话只属于一个线程、事件循环和生命周期任务。"""
import asyncio
import concurrent.futures
import json
import logging
import threading
from datetime import timedelta

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from app.core.rag_context import current_budget, phase

log = logging.getLogger(__name__)


class KnowledgeMCPClient:
    def __init__(self, server: dict, *, cwd: str, concurrency: int):
        self.parameters = StdioServerParameters(
            command=server["command"], args=server.get("args", []),
            env=server.get("env"), cwd=cwd,
        )
        self.concurrency = concurrency
        self.ready = concurrent.futures.Future()
        self.loop = None
        self.session = None
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name="knowledge-mcp")

    async def start(self):
        self.thread.start()
        try:
            return await asyncio.wait_for(asyncio.wrap_future(self.ready), 60)
        except BaseException:
            await self.close()
            raise

    def _run(self):
        asyncio.run(self._lifecycle())

    async def _lifecycle(self):
        self.loop = asyncio.get_running_loop()
        self.stop = asyncio.Event()
        self.disconnected = asyncio.Event()
        self.slots = asyncio.Semaphore(self.concurrency)
        self.calls = set()
        while not self.stop.is_set():
            try:
                async with stdio_client(self.parameters) as (reader, writer):
                    async with ClientSession(reader, writer) as session:
                        async with asyncio.timeout(55):
                            await session.initialize()
                            discovered = await session.list_tools()
                        self.disconnected.clear()
                        self.session = session
                        if not self.ready.done():
                            self.ready.set_result(discovered.tools)
                        try:
                            while not self.stop.is_set() and not self.disconnected.is_set():
                                try:
                                    await asyncio.wait_for(self.stop.wait(), 1)
                                except TimeoutError:
                                    async with asyncio.timeout(2):
                                        await session.send_ping()
                        finally:
                            self.session = None
                            # 旧连接的请求不排入新连接，避免重放查询和日志写入。
                            pending = list(self.calls)
                            for task in pending:
                                task.cancel()
                            await asyncio.gather(*pending, return_exceptions=True)
            except Exception as exc:
                self.session = None
                log.warning("knowledge_mcp_connection_failed error_type=%s", type(exc).__name__)
                if not self.ready.done():
                    self.ready.set_exception(RuntimeError("知识 MCP 连接初始化失败"))
                    return
            if not self.stop.is_set():
                try:
                    await asyncio.wait_for(self.stop.wait(), 1)
                except TimeoutError:
                    pass

    async def close(self):
        if self.loop is not None and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.stop.set)
        if self.thread.ident is not None:
            await asyncio.to_thread(self.thread.join)

    def handler(self, arguments: dict) -> str:
        budget = current_budget()
        if budget is None or self.loop is None or not self.loop.is_running():
            return json.dumps({"error": "知识 MCP 连接不可用"})
        future = None
        try:
            timeout = budget.remaining()
            future = asyncio.run_coroutine_threadsafe(self._call(arguments), self.loop)
            return future.result(timeout=timeout)
        except Exception:
            if future is not None:
                future.cancel()
            return json.dumps({"error": "知识 MCP 查询失败或超时"})

    async def _call(self, arguments):
        budget = current_budget()
        session = self.session
        if budget is None or session is None:
            raise RuntimeError("知识 MCP 连接不可用")
        task = asyncio.current_task()
        self.calls.add(task)
        acquired = False
        try:
            async with asyncio.timeout(budget.remaining()):
                with phase("mcp_wait"):
                    await self.slots.acquire()
                    acquired = True
                if session is not self.session or self.disconnected.is_set():
                    raise RuntimeError("知识 MCP 连接已变更")
                try:
                    result = await session.call_tool(
                        "knowledge_search", arguments,
                        read_timeout_seconds=timedelta(seconds=budget.remaining()),
                    )
                except TimeoutError:
                    raise
                except Exception:
                    self.disconnected.set()
                    raise
                if result.isError:
                    return json.dumps({"error": "知识 MCP 工具执行失败"})
                payload = result.structuredContent
                if payload is None:
                    text = "".join(item.text for item in result.content if item.type == "text")
                    payload = json.loads(text)
                return json.dumps({"structuredContent": payload}, ensure_ascii=False)
        finally:
            if acquired:
                self.slots.release()
            self.calls.discard(task)
