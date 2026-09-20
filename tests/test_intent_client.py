import json
from types import SimpleNamespace

import httpx
from openai import OpenAI
import pytest

from app.hermes import intent_client


@pytest.mark.parametrize("timeout_seconds", [0.5, 10])
@pytest.mark.parametrize("content,finish,valid", [
    ('{"intent":"attendance_query","confidence":1,"extracted_slots":{"query_month":"2026-08","query_type":"late_count"}}', "stop", True),
    ("", "stop", False),
    ("not-json", "stop", False),
    ("[]", "stop", False),
    ('{"intent":"attendance_query"}', "length", False),
    ('{"intent":"attendance_query"}', "tool_calls", False),
])
def test_classification_uses_one_sdk_request_without_tools(monkeypatch, content, finish, valid, timeout_seconds):
    requests = []
    def respond(request):
        assert request.extensions["timeout"]["connect"] == min(5.0, timeout_seconds)
        assert request.extensions["timeout"]["read"] == timeout_seconds
        body = json.loads(request.content)
        requests.append(body)
        assert "tools" not in body and "tool_choice" not in body
        assert body["messages"] == [
            {"role": "system", "content": "只输出 JSON"},
            {"role": "user", "content": "统计一下8月份的迟到和早退"},
        ]
        assert body["response_format"] == {"type": "json_object"}
        assert body["stream"] is False
        return httpx.Response(200, json={
            "id": "test", "object": "chat.completion", "created": 1, "model": "test",
            "choices": [{"index": 0, "finish_reason": finish,
                         "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        })
    clients = []
    def factory(**kwargs):
        assert kwargs["max_retries"] == 0  # OpenAI SDK 的 0 表示首次请求后不重试。
        client = OpenAI(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(respond)))
        clients.append(client)
        return client
    monkeypatch.setattr(intent_client, "OpenAI", factory)
    settings = SimpleNamespace(dashscope_api_key="test", intent_timeout_seconds=timeout_seconds)
    if valid:
        result = intent_client.classify_intent(settings, "统计一下8月份的迟到和早退", "只输出 JSON")
        assert json.loads(result["final_response"])["intent"] == "attendance_query"
    else:
        with pytest.raises(RuntimeError):
            intent_client.classify_intent(settings, "统计一下8月份的迟到和早退", "只输出 JSON")
    assert len(requests) == 1
    assert all(client.is_closed() for client in clients)
