import asyncio
import json
import logging

from tools.registry import registry


SERVER_NAME = "oa_material_requisition_mcp"
logger = logging.getLogger(__name__)


class RequisitionMCPError(RuntimeError):
    pass


async def call_requisition_tool(
    tool_name: str,
    arguments: dict,
) -> dict:
    registry_name = f"mcp__{SERVER_NAME}__{tool_name}"

    if registry.get_entry(registry_name) is None:
        logger.error(
            "requisition_mcp_tool_not_registered tool=%s",
            registry_name,
        )
        raise RequisitionMCPError(
            f"物资申领 MCP 工具未注册: {tool_name}"
        )

    try:
        raw = await asyncio.to_thread(
            registry.dispatch,
            registry_name,
            arguments,
        )
    except Exception as exc:
        logger.exception(
            "requisition_mcp_call_failed tool=%s",
            registry_name,
        )
        raise RequisitionMCPError(
            f"物资申领 MCP 调用失败: {exc}"
        ) from exc

    try:
        envelope = (
            json.loads(raw)
            if isinstance(raw, str)
            else raw
        )

        if not isinstance(envelope, dict):
            raise ValueError

        if envelope.get("error"):
            logger.error(
                "requisition_mcp_returned_error tool=%s error=%s",
                registry_name,
                envelope["error"],
            )
            raise RequisitionMCPError(
                str(envelope["error"])
            )

        payload = envelope.get("structuredContent")
        if payload is None:
            payload = envelope.get("result")
        if payload is None:
            payload = envelope

        if isinstance(payload, str):
            payload = json.loads(payload)

        if not isinstance(payload, dict):
            raise ValueError

        return payload
    except RequisitionMCPError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        logger.exception(
            "requisition_mcp_invalid_response tool=%s response_type=%s",
            registry_name,
            type(raw).__name__,
        )
        raise RequisitionMCPError(
            "物资申领 MCP 返回格式错误"
        ) from exc
