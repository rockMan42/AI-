"""意图分类只需一次 JSON 补全，不进入通用 Agent 工具循环。"""
import json

import httpx
from openai import OpenAI

from app.core.rag_context import phase
from app.hermes.agent import AGENT_MODEL, AGENT_BASE_URL


def classify_intent(
    settings,
    user_message: str,
    system_message: str,
    *,
    model: str | None = None,
    max_tokens: int = 512,
) -> dict:
    with OpenAI(
        api_key=settings.dashscope_api_key,
        base_url=AGENT_BASE_URL,
        max_retries=0,
        timeout=httpx.Timeout(settings.intent_timeout_seconds, connect=1.0),
    ) as client:
        with phase("intent_completion") as metrics:
            response = client.chat.completions.create(
                model=model or AGENT_MODEL,
                messages=[
                    {"role": "system", "content": system_message},
                    {"role": "user", "content": user_message},
                ],
                response_format={"type": "json_object"},
                stream=False,
                max_tokens=max_tokens,
            )
            if response.usage is not None:
                metrics["input_tokens"] = response.usage.prompt_tokens
                metrics["output_tokens"] = response.usage.completion_tokens
            if not response.choices:
                raise RuntimeError("意图分类未返回结果")
            choice = response.choices[0]
            if choice.message.tool_calls or choice.finish_reason != "stop":
                raise RuntimeError("意图分类未返回完整 JSON")
            content = choice.message.content or ""
            try:
                parsed = json.loads(content)
            except ValueError:
                raise RuntimeError("意图分类返回了无效 JSON") from None
            if not isinstance(parsed, dict):
                raise RuntimeError("意图分类返回格式无效")
            return {"final_response": content}
