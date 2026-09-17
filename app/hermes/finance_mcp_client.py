import asyncio
import json

from tools.registry import registry

from app.security.expense import issue_finance_token


SERVER_NAME = "finance_expense_mcp"


class FinanceMCPError(RuntimeError):
    pass


def decode_response(raw) -> dict:
    envelope = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(envelope, dict):
        raise FinanceMCPError("财务 MCP 响应格式错误")

    if envelope.get("error") or envelope.get("isError"):
        raise FinanceMCPError("财务 MCP 返回失败")

    payload = envelope.get("structuredContent")
    if payload is None:
        payload = envelope.get("result")

    if payload is None:
        texts = [
            item.get("text", "")
            for item in envelope.get("content", [])
            if item.get("type") == "text"
        ]
        if texts:
            payload = "".join(texts)

    if payload is None:
        payload = envelope

    if isinstance(payload, str):
        payload = json.loads(payload)

    if not isinstance(payload, dict):
        raise FinanceMCPError("财务 MCP 业务数据格式错误")
    return payload


async def call_finance_tool(
    tool_name: str,
    user_id: int,
    expense_id: int,
) -> dict:
    if tool_name not in {"submit_expense", "query_expense"}:
        raise FinanceMCPError("不支持的财务工具")

    name = f"mcp__{SERVER_NAME}__{tool_name}"
    if registry.get_entry(name) is None:
        raise FinanceMCPError("财务 MCP 工具未注册")

    arguments = {
        "expense_id": expense_id,
        "identity_token": issue_finance_token(user_id, expense_id),
    }

    try:
        async with asyncio.timeout(20):
            raw = await asyncio.to_thread(
                registry.dispatch,
                name,
                arguments,
            )
        return decode_response(raw)
    except FinanceMCPError:
        raise
    except Exception:
        raise FinanceMCPError("财务提交结果暂未确认") from None

