from unittest.mock import AsyncMock

import httpx
import pytest

from app.services.conversation_engine import feishu


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["text", "card", "notification", "update"])
async def test_send_uses_shared_connection_timeout(monkeypatch, operation):
    requests = []

    def respond(request):
        requests.append(request)
        assert request.extensions["timeout"]["connect"] == 5.0
        assert request.extensions["timeout"]["read"] == 5.0
        return httpx.Response(200, json={
            "code": 0, "data": {"message_id": "test-message"},
        })

    client_class = httpx.AsyncClient

    def factory(**kwargs):
        return client_class(
            **kwargs, transport=httpx.MockTransport(respond),
        )

    monkeypatch.setattr(feishu.httpx, "AsyncClient", factory)
    monkeypatch.setattr(feishu, "_get_tenant_access_token", AsyncMock(return_value="test"))
    monkeypatch.setattr(feishu, "wait_feishu_send_slot", AsyncMock())
    await feishu.init_feishu_client()
    client = feishu.get_feishu_client()
    try:
        if operation == "text":
            await feishu._send_feishu_reply("test-message", "测试")
        elif operation == "card":
            await feishu._send_feishu_card_reply("test-message", {})
        elif operation == "notification":
            assert await feishu.send_feishu_card("test-user", {}, "test-uuid") == "test-message"
        else:
            await feishu.update_feishu_card("test-message", {})
        assert len(requests) == 1
    finally:
        await feishu.close_feishu_client()
    assert client.is_closed
