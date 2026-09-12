import asyncio
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal
import threading
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI
from redis import RedisError

from app.schemas.attendance import AttendanceQuery, month_bounds
from app.schemas.skill_result import SkillResult
from app.services.conversation_engine.attendance_fast_path import parse_attendance_query, can_use_fast_path
from app.services.conversation_engine.model_runner import ModelRunner
from app.services.conversation_engine.slot_manage import SessionSlots
from app.security.attendance import authorize_attendance, AttendanceTarget
from app.services.attendance import attendance_service as service_module
from app.services.attendance.attendance_service import AttendanceService


@pytest.mark.parametrize("text,kind,field,value", [
    ("查本月考勤", "all", "query_month", "2026-01"),
    ("请帮我查上月考勤，谢谢！", "all", "query_month", "2025-12"),
    ("查昨天打卡", "punch_record", "query_date", "2025-12-31"),
    ("查今天打卡", "punch_record", "query_date", "2026-01-01"),
    ("查上月早退次数", "late_count", "query_month", "2025-12"),
    ("查本月迟到次数", "late_count", "query_month", "2026-01"),
    ("查假期余额", "leave_balance", "query_year", 2026),
    ("查今年假期余额", "leave_balance", "query_year", 2026),
    ("查去年假期余额", "leave_balance", "query_year", 2025),
    ("查25年假期余额", "leave_balance", "query_year", 2025),
    ("查2025年假期余额", "leave_balance", "query_year", 2025),
])
def test_fast_path(text, kind, field, value):
    parsed = parse_attendance_query(text, date(2026, 1, 1))
    assert parsed["extracted_slots"][field] == value
    assert parsed["extracted_slots"]["query_type"] == kind


@pytest.mark.parametrize("text", ["申请年假", "不要查考勤", "查张三的余额", "查本月考勤和年假",
    "查本月考勤只看迟到", "查本月考勤但不是我的", "查2025年的天气", "查0000年假期余额"])
def test_fast_path_rejects_ambiguous(text):
    assert parse_attendance_query(text, date(2026, 1, 1)) is None


def test_context_gate():
    session = SessionSlots(session_id="test", user_id=1)
    assert can_use_fast_path(session)
    session.intent_code = "attendance_query"
    session.workflow_state = "executing"
    assert not can_use_fast_path(session)
    session.workflow_state = "completed"
    assert can_use_fast_path(session)
    session.suspended_contexts = [{"intent_code": "leave_apply"}]
    assert not can_use_fast_path(session)


@pytest.mark.asyncio
async def test_timeout_keeps_slot_until_thread_finishes():
    runner = ModelRunner(1, .02, .02)
    release = threading.Event()
    try:
        with pytest.raises(TimeoutError):
            await runner.run(lambda: release.wait(2))
        with pytest.raises(TimeoutError):
            await runner.run(lambda: "must not run")
        assert len(runner.tasks) == 1
    finally:
        release.set()
        await runner.close()
    assert await runner.run(lambda: "ok") == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("role,department,target_id,allowed", [
    ("员工", 1, 3, True), ("员工", 1, 4, False),
    ("HR", 1, 4, True), ("HR", 2, 4, False), ("管理员", 1, 4, False),
])
async def test_authorization(role, department, target_id, allowed):
    actor = NS(user_id=3, name="test", role=role, department_id=department, status="活动")
    target = NS(user_id=4, name="target", department_id=1, status="活动")
    db = NS(scalar=AsyncMock(side_effect=[actor, target]))
    if allowed:
        assert (await authorize_attendance(db, "trusted", target_id)).user_id == target_id
    else:
        with pytest.raises(PermissionError):
            await authorize_attendance(db, "trusted", target_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,expected", [
    ("punch_record", {"punch_records"}), ("late_count", {"late_stats"}),
    ("leave_balance", {"leave_balances"}), ("all", {"punch_records", "late_stats", "leave_balances"}),
])
async def test_authorization_closed_before_workers(monkeypatch, kind, expected):
    closed = False
    @asynccontextmanager
    async def factory():
        nonlocal closed
        yield NS(connection=AsyncMock())
        closed = True
    monkeypatch.setattr(service_module, "authorize_attendance", AsyncMock(return_value=AttendanceTarget(3, "test")))
    service = AttendanceService(factory)
    calls = set()
    def worker(name):
        async def run(*args):
            assert closed
            calls.add(name)
            if name == "leave_balances":
                assert args == (3, 2025)
            return []
        return run
    service._punch_records = worker("punch_records")
    service._late_stats = worker("late_stats")
    service._leave_balances = worker("leave_balances")
    result = await service.query(AttendanceQuery(user_id="3", query_type=kind, year=2025), "trusted")
    assert calls == expected
    assert result["leave_year"] == 2025


@pytest.mark.asyncio
async def test_cache_failure_falls_back(monkeypatch):
    monkeypatch.setattr(service_module.redis_module, "redis_client", NS(get=AsyncMock(side_effect=RedisError), set=AsyncMock(side_effect=RedisError)))
    service = AttendanceService()
    assert await service._read_stats_cache("test") is None
    monkeypatch.setattr(service_module.redis_module.redis_client, "get", AsyncMock(return_value="not json"))
    assert await service._read_stats_cache("test") is None


@pytest.mark.asyncio
async def test_card_completed_before_return(monkeypatch):
    from app.api.v1 import feishu_gateway_webhook as webhook
    manager = NS(get_or_create_session=AsyncMock(return_value=NS(session_id="test")), update_workflow_state=AsyncMock())
    monkeypatch.setattr(webhook, "conversation_manager", manager)
    monkeypatch.setattr(webhook, "slot_collector", NS(collect=AsyncMock(return_value={"action": "execute", "slots": {}})))
    executor = NS(executor=AsyncMock(return_value=SkillResult(True, "ok", card={"header": {}})))
    monkeypatch.setattr(webhook, "ExecutorRegistry", lambda: NS(get_executor=lambda _: executor))
    monkeypatch.setattr(webhook, "get_agent", Mock(side_effect=AssertionError("must not create agent")))
    user = NS(user_id=3, feishu_open_id="test", role="员工", department_id=1)
    result = await webhook.handle_skill_action(user, "查本月考勤", "test", {"skill": NS(name="attendance_query"), "slots": {}, "confidence": 1}, None, None)
    assert result == {"header": {}}
    assert manager.update_workflow_state.call_args.args[1].value == "completed"


@pytest.mark.asyncio
async def test_card_sends_once(monkeypatch):
    from app.api.v1 import feishu_gateway_webhook as webhook
    card, text = AsyncMock(), AsyncMock()
    monkeypatch.setattr(webhook, "_send_feishu_card_reply", card)
    monkeypatch.setattr(webhook, "_send_feishu_reply", text)
    await webhook.send_business_reply("test", {"header": {}}, "p2p")
    card.assert_awaited_once()
    text.assert_not_awaited()
    await webhook.send_business_reply("test", {"header": {}}, "group")
    assert card.await_count == 1
    text.assert_awaited_once()


@pytest.mark.asyncio
async def test_feishu_client_reuse_and_token_refresh(monkeypatch):
    from app.services.conversation_engine import feishu
    await feishu.init_feishu_client()
    original = feishu.get_feishu_client()
    await feishu.init_feishu_client()
    assert feishu.get_feishu_client() is original
    cache = {}
    async def get(key): return cache.get(key)
    async def set_(key, value, ttl): cache[key] = value
    monkeypatch.setattr(feishu, "get_cache", get)
    monkeypatch.setattr(feishu, "set_cache", set_)
    post = AsyncMock(return_value=httpx.Response(200, json={"code": 0, "tenant_access_token": "test", "expire": 7200}, request=httpx.Request("POST", "https://example.invalid")))
    monkeypatch.setattr(original, "post", post)
    assert await asyncio.gather(*(feishu._get_tenant_access_token() for _ in range(10))) == ["test"]*10
    post.assert_awaited_once()
    await feishu.close_feishu_client()
    assert original.is_closed


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["punch-records", "late-stats", "leave-balance"])
async def test_http_forbidden(monkeypatch, endpoint):
    from app.api.v1 import attendance
    app = FastAPI()
    app.include_router(attendance.router)
    app.dependency_overrides[attendance.attendance_actor] = lambda: NS(user_id=3, feishu_open_id="test")
    query = AsyncMock(side_effect=PermissionError)
    monkeypatch.setattr(attendance, "AttendanceService", lambda: NS(query=query))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/attendance/{endpoint}", params={"user_id": "4", "year": 2025})
    assert response.status_code == 403
    assert response.json() == {"detail": "无权查询该员工的考勤"}


def test_month_bounds():
    assert month_bounds("2025-12") == (date(2025, 12, 1), date(2026, 1, 1))


@pytest.mark.asyncio
async def test_punch_pagination_and_month_sql():
    from sqlalchemy.dialects import mysql
    statements = []
    rows = [NS(date=date(2025, 8, 20-i), clock_in_time=None, clock_out_time=None,
               status="正常", work_hours=Decimal("0")) for i in range(6)]
    async def scalars(statement):
        statements.append(statement)
        return NS(all=lambda: rows)
    @asynccontextmanager
    async def factory():
        yield NS(connection=AsyncMock(), scalars=scalars)
    result = await AttendanceService(factory)._punch_records(
        AttendanceQuery(user_id="3", month="2025-08", status_filter="正常"), 3)
    assert len(result["items"]) == 5 and result["has_more"]
    assert result["items"][0]["work_hours"] == "0"
    sql = str(statements[0].compile(dialect=mysql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "2025-09-01" in sql and "t_attendance.date <" in sql
    assert "ORDER BY t_attendance.date DESC" in sql


@pytest.mark.asyncio
async def test_balance_missing_is_not_zero():
    row = NS(leave_type="annual", total_days=Decimal("10"), used_days=Decimal("5"), remaining_days=Decimal("5"))
    @asynccontextmanager
    async def factory():
        yield NS(connection=AsyncMock(), scalars=AsyncMock(return_value=NS(all=lambda: [row])))
    balances = await AttendanceService(factory)._leave_balances(3, 2025)
    assert next(x for x in balances if x["leave_type"] == "annual")["remaining_days"] == "5"
    assert next(x for x in balances if x["leave_type"] == "compensatory")["remaining_days"] is None


@pytest.mark.asyncio
async def test_completed_session_does_not_retain_previous_year():
    from app.services.conversation_engine.conversation_manager import ConversationManager
    from app.services.conversation_engine.register_skill import SkillRegistry
    from app.services.conversation_engine.slot_collector import SlotCollector
    from app.constant.intent_state import IntentState
    class Store:
        session = None
        async def load(self, key): return self.session
        async def save(self, value): self.session = value
    store = Store()
    manager, collector = ConversationManager(store), SlotCollector(store)
    register = SkillRegistry()
    register.load_from_directory("app/hermes/skills")
    skill = register.get_skill("attendance_query")
    session = await manager.get_or_create_session(3, skill.name, skill)
    await collector.collect(session, skill, {"query_year": 2025, "target_user_id": "4"})
    await manager.update_workflow_state("3", IntentState.SKILL_COMPLETED)
    session = await manager.get_or_create_session(3, skill.name, skill)
    result = await collector.collect(session, skill, {"query_type": "leave_balance"})
    assert "query_year" not in result["slots"]
    assert "target_user_id" not in result["slots"]


@pytest.mark.asyncio
async def test_webhook_fast_path_skips_model(monkeypatch):
    from app.api.v1 import feishu_gateway_webhook as webhook
    from app.services.conversation_engine.register_skill import SkillRegistry
    import json
    monkeypatch.setattr(webhook.settings, "attendance_fast_path_enabled", True)
    monkeypatch.setattr(webhook.settings, "feishu_allowed_users", "test")
    monkeypatch.setattr(webhook, "_verify_signature", AsyncMock(return_value=True))
    monkeypatch.setattr(webhook, "set_cache_if_absent", AsyncMock(return_value=True))
    user = NS(user_id=3, status="活动")
    @asynccontextmanager
    async def factory(): yield NS(expunge=Mock())
    monkeypatch.setattr(webhook, "create_session", factory)
    monkeypatch.setattr(webhook, "get_or_create_user", AsyncMock(return_value=user))
    monkeypatch.setattr(webhook, "conversation_manager", NS(update_intent_state=AsyncMock(return_value=SessionSlots("3", 3))))
    monkeypatch.setattr(webhook, "try_retry_failed_skill", AsyncMock(return_value=None))
    monkeypatch.setattr(webhook, "try_handle_pending_slot", AsyncMock(return_value=None))
    handle, send = AsyncMock(return_value={"header": {}}), AsyncMock()
    monkeypatch.setattr(webhook, "handle_skill_action", handle)
    monkeypatch.setattr(webhook, "send_business_reply", send)
    monkeypatch.setattr(webhook, "conversation", AsyncMock(side_effect=AssertionError("must skip model")))
    monkeypatch.setattr(webhook, "classify_intent", Mock(side_effect=AssertionError("must skip classifier")))
    monkeypatch.setattr(webhook, "get_agent", Mock(side_effect=AssertionError("must skip agent")))
    register = SkillRegistry()
    register.load_from_directory("app/hermes/skills")
    request = NS(json=AsyncMock(return_value={"header": {"token": "test"}, "event": {
        "sender": {"sender_id": {"open_id": "test"}},
        "message": {"message_id": "test", "message_type": "text", "chat_type": "p2p",
                    "content": json.dumps({"text": "查25年假期余额"})}}}))
    await webhook.feishu_webhook(request, db=None, register=register)
    assert handle.call_args.kwargs["router_result"]["slots"]["query_year"] == 2025
    send.assert_awaited_once()


@pytest.mark.asyncio
async def test_model_sdk_timeout_and_cleanup(monkeypatch):
    from app.api.v1 import feishu_gateway_webhook as webhook
    create = Mock(return_value={})
    client = NS(chat=NS(completions=NS(create=create)), close=Mock())
    def run(**kwargs):
        # 与当前 Hermes conversation_loop 一致：计数包含首次尝试。
        for _ in range(agent._api_max_retries):
            client.chat.completions.create(timeout=1800)
            return {"final_response": "ok"}
        return {"final_response": ""}
    agent = NS(client=client, run_conversation=run)
    monkeypatch.setattr(webhook, "get_agent", Mock(return_value=agent))
    monkeypatch.setattr(webhook, "model_runner", ModelRunner())
    assert await webhook.conversation(None, "test", "test", "test") == {"final_response": "ok"}
    assert create.call_args.kwargs["timeout"].read <= 10
    create.assert_called_once()
    assert client.chat.completions.create is create
    client.close.assert_called_once()


@pytest.mark.asyncio
async def test_empty_model_response_is_failure(monkeypatch, caplog):
    from app.api.v1 import feishu_gateway_webhook as webhook
    create = Mock()
    client = NS(chat=NS(completions=NS(create=create)), close=Mock())
    agent = NS(client=client, run_conversation=Mock(return_value={"final_response": ""}))
    monkeypatch.setattr(webhook, "get_agent", Mock(return_value=agent))
    monkeypatch.setattr(webhook, "model_runner", ModelRunner())
    with caplog.at_level("INFO"), pytest.raises(RuntimeError, match="模型未返回有效回复"):
        await webhook.conversation(None, "test", "test", "test")
    assert any("stage=model_execution" in record.message and "outcome=error" in record.message for record in caplog.records)
    client.close.assert_called_once()


def test_tool_timeout_cancels_correct_future(monkeypatch):
    from app.hermes.tools import attendance_tool as tool
    from concurrent.futures import TimeoutError
    future = NS(result=Mock(side_effect=TimeoutError), cancel=Mock())
    def submit(coroutine, loop):
        coroutine.close()
        return future
    register = Mock()
    monkeypatch.setattr(tool.asyncio, "run_coroutine_threadsafe", submit)
    monkeypatch.setattr(tool.registry, "register", register)
    tool.register_attendance_tool(None)
    import json
    result = json.loads(register.call_args.kwargs["handler"]({}, actor_open_id="test"))
    assert result["status_code"] == 504
    future.cancel.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("skill_name", ["policy_query", "leave_apply"])
async def test_non_card_skill_reply_regression(monkeypatch, skill_name):
    from app.api.v1 import feishu_gateway_webhook as webhook
    manager = NS(get_or_create_session=AsyncMock(return_value=NS(session_id="test")), update_workflow_state=AsyncMock())
    monkeypatch.setattr(webhook, "conversation_manager", manager)
    monkeypatch.setattr(webhook, "slot_collector", NS(collect=AsyncMock(return_value={"action": "execute", "slots": {}})))
    executor = NS(executor=AsyncMock(return_value=SkillResult(True, "业务结果")))
    monkeypatch.setattr(webhook, "ExecutorRegistry", lambda: NS(get_executor=lambda _: executor))
    conversation = AsyncMock(return_value={"final_response": "业务结果"})
    monkeypatch.setattr(webhook, "conversation", conversation)
    user = NS(user_id=3, feishu_open_id="test", role="员工", department_id=1)
    result = await webhook.handle_skill_action(user, "test", "test", {"skill": NS(name=skill_name), "slots": {}, "confidence": 1}, None, None)
    assert result == "业务结果"
    assert conversation.await_count == (1 if skill_name == "leave_apply" else 0)
    assert manager.update_workflow_state.call_args.args[1].value == "completed"


@pytest.mark.asyncio
async def test_parallel_requests_keep_identity_and_logs_private(monkeypatch, caplog):
    @asynccontextmanager
    async def factory(): yield NS(connection=AsyncMock())
    async def authorize(db, actor, target):
        assert actor == f"actor-{target}"
        return AttendanceTarget(target, "private-name")
    monkeypatch.setattr(service_module, "authorize_attendance", authorize)
    service = AttendanceService(factory)
    async def balances(user_id, year):
        await asyncio.sleep(0)
        return [{"private-balance": user_id}]
    service._leave_balances = balances
    with caplog.at_level("INFO"):
        results = await asyncio.gather(*(service.query(AttendanceQuery(user_id=str(i), query_type="leave_balance"), f"actor-{i}") for i in (3, 4)))
    assert [x["leave_balances"][0]["private-balance"] for x in results] == [3, 4]
    assert "private-name" not in caplog.text and "private-balance" not in caplog.text
