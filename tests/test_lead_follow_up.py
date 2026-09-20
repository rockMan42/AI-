from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api.v1 import feishu_gateway_webhook as webhook
from app.schemas.lead import FollowUpInput
from app.services.conversation_engine.register_skill import SkillRegistry
from app.services.conversation_engine.slot_collector import SlotCollector
from app.services.conversation_engine.slot_manage import SessionSlots, SlotState


MESSAGE = "本次通过微信跟进客户线索，下一步行动是签约，下次跟进时间是2026年11月30日"


@pytest.fixture
def registry():
    result = SkillRegistry()
    result.load_from_directory(str(Path(__file__).resolve().parents[1] / "app/hermes/skills"))
    return result


def session(pending="content"):
    return SessionSlots(
        session_id="test", user_id=1, intent_code="lead_follow_up",
        pending_slot=pending,
        slots={pending: SlotState(name=pending, required=True)},
    )


@pytest.mark.asyncio
async def test_follow_up_continues_without_reclassification(monkeypatch, registry):
    execute = AsyncMock(return_value="saved")
    monkeypatch.setattr(webhook, "handle_skill_action", execute)
    result = await webhook.try_handle_pending_slot(
        session(), SimpleNamespace(user_id=1), MESSAGE, "test-message",
        registry, None, None,
    )
    assert result == "saved"
    slots = execute.call_args.kwargs["router_result"]["slots"]
    assert slots == {
        "content": "本次通过微信跟进客户线索",
        "next_action": "签约",
        "next_follow_up": "2026-11-30T00:00:00",
    }
    body = FollowUpInput(follow_up_type="wechat", **slots)
    assert body.next_follow_up.isoformat() == "2026-11-30T00:00:00+08:00"


@pytest.mark.parametrize("text", ["查一下我的客户", "请帮我查询客户", "我要报销"])
def test_explicit_switch_still_works(registry, text):
    assert webhook.has_explicit_intent_switch(text, registry, "lead_follow_up")


@pytest.mark.parametrize("date_text", ["2026年02月30日", "下周一", ""])
@pytest.mark.asyncio
async def test_invalid_date_does_not_submit(monkeypatch, registry, date_text):
    execute = AsyncMock()
    monkeypatch.setattr(webhook, "handle_skill_action", execute)
    current = session()
    result = await webhook.try_handle_pending_slot(
        current, SimpleNamespace(user_id=1),
        f"客户已确认方案，下次跟进时间是{date_text}",
        "test-message", registry, None, None,
    )
    assert "下次跟进时间" in result
    execute.assert_not_awaited()
    assert current.pending_slot == "content"


def test_wechat_and_plain_content():
    collector = SlotCollector(None)
    assert collector.parse_pending_slot_reply(
        session("follow_up_type"), "通过微信",
    ) == {"follow_up_type": "wechat"}
    assert collector.parse_pending_slot_reply(
        session(), "客户已经确认方案",
    ) == {"content": "客户已经确认方案"}


def test_explicit_outcome_and_time_are_preserved():
    result = SlotCollector(None).parse_pending_slot_reply(
        session(),
        "客户确认方案，跟进结果：积极；下一步行动：签约；下次跟进时间：2026-11-30T15:30:00+08:00",
    )
    assert result["outcome"] == "positive"
    assert result["next_action"] == "签约"
    assert result["next_follow_up"] == "2026-11-30T15:30:00+08:00"


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["这次", "本次", ""])
async def test_complete_reply_at_method_step_reaches_executor(monkeypatch, registry, prefix):
    from app.skill_executor import lead as lead_executor

    skill = registry.get_skill("lead_follow_up")
    current = SessionSlots(
        session_id="1", user_id=1, intent_code=skill.name,
        pending_slot="follow_up_type", status="collecting",
        slots={
            "lead_id": SlotState(name="lead_id", value=123, filled=True),
            "follow_up_type": SlotState(name="follow_up_type", required=True),
        },
    )
    manager = SimpleNamespace(
        get_or_create_session=AsyncMock(return_value=current),
        update_workflow_state=AsyncMock(),
    )
    store = SimpleNamespace(save=AsyncMock())
    monkeypatch.setattr(webhook, "conversation_manager", manager)
    monkeypatch.setattr(webhook, "slot_collector", SlotCollector(store))
    mcp = AsyncMock(return_value={
        "id": 456, "next_follow_up": "2026-11-30T00:00:00+08:00",
    })
    monkeypatch.setattr(lead_executor, "call_crm_tool", mcp)
    user = SimpleNamespace(
        user_id=1, feishu_open_id="test", role="员工", department_id=1,
    )
    message = f"{prefix}通过微信跟进客户线索，下一步行动是签约，下次跟进时间是2026年11月30日"
    result = await webhook.try_handle_pending_slot(
        current, user, message, "test-message", registry, None, None,
    )
    assert isinstance(result, dict)
    mcp.assert_awaited_once()
    tool, user_id, arguments = mcp.call_args.args
    assert (tool, user_id, arguments["lead_id"]) == ("update_follow_up", 1, 123)
    assert arguments["body"]["follow_up_type"] == "wechat"
    assert arguments["body"]["content"] == f"{prefix}通过微信跟进客户线索"
    assert arguments["body"]["next_action"] == "签约"
    assert arguments["body"]["next_follow_up"] == "2026-11-30T00:00:00+08:00"


@pytest.mark.asyncio
async def test_invalid_complete_reply_at_method_step_does_not_execute(monkeypatch, registry):
    execute = AsyncMock()
    monkeypatch.setattr(webhook, "handle_skill_action", execute)
    result = await webhook.try_handle_pending_slot(
        session("follow_up_type"), SimpleNamespace(user_id=1),
        "这次通过微信跟进客户线索，下次跟进时间是2026年02月30日",
        "test-message", registry, None, None,
    )
    assert "下次跟进时间无法识别" in result
    execute.assert_not_awaited()
