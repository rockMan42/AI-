import asyncio
import json

from tools.registry import registry

from app.security.lead import issue_crm_token


SERVER_NAME = "crm_lead_mcp"
TOOL_NAMES = (
    "query_leads",
    "get_lead_detail",
    "update_follow_up",
)


class CRMMCPError(RuntimeError):
    pass


def decode_response(raw) -> dict:
    envelope = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(envelope, dict):
        raise CRMMCPError("CRM MCP 响应格式错误")

    if envelope.get("error") or envelope.get("isError"):
        raise CRMMCPError("CRM 操作失败，请检查权限和输入信息")

    payload = envelope.get("structuredContent")
    if payload is None:
        payload = envelope.get("result")
    if payload is None:
        texts = [
            item.get("text", "")
            for item in envelope.get("content", [])
            if item.get("type") == "text"
        ]
        payload = "".join(texts) if texts else envelope

    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        raise CRMMCPError("CRM MCP 业务数据格式错误")
    return payload


async def call_crm_tool(
    tool_name: str,
    user_id: int,
    arguments: dict,
) -> dict:
    if tool_name not in TOOL_NAMES:
        raise CRMMCPError("不支持的 CRM 工具")

    name = f"mcp__{SERVER_NAME}__{tool_name}"
    if registry.get_entry(name) is None:
        raise CRMMCPError("CRM MCP 工具未注册")

    arguments = {
        **arguments,
        "identity_token": issue_crm_token(
            user_id, tool_name, arguments.get("lead_id"),
        ),
    }

    try:
        async with asyncio.timeout(20):
            raw = await asyncio.to_thread(
                registry.dispatch, name, arguments,
            )
        return decode_response(raw)
    except CRMMCPError:
        raise
    except Exception:
        raise CRMMCPError(
            "CRM 调用结果暂未确认；提交跟进后请先查看记录，避免重复提交"
        ) from None