import asyncio
import json
import os
import sys
from pathlib import Path
from app.hermes.crm_mcp_client import (
    SERVER_NAME as CRM_SERVER_NAME,
    TOOL_NAMES as CRM_TOOL_NAMES,
)
import yaml
from pydantic import SecretStr
from run_agent import AIAgent
from tools.mcp_tool import register_mcp_servers, shutdown_mcp_servers
from app.hermes.tools.attendance_tool import (
    TOOLSET as ATTENDANCE_TOOLSET,
    register_attendance_tool,
)
from app.config.settings import Settings
from app.core.rag_client import RAGError
from app.core.rag_context import query_scope, phase
from app.hermes.knowledge_mcp_client import KnowledgeMCPClient
from app.models import User
from app.schemas.knowledge import KnowledgeSearchRequest, KnowledgeSearchResponse
from app.security.knowledge import issue_identity_token
from tools.registry import registry


FINANCE_SERVER_NAME = "finance_expense_mcp"
FINANCE_TOOL_NAMES = (
    "submit_expense",
    "query_expense",
    "query_expense_status",
)

_agent_settings: Settings | None = None
_knowledge_client: KnowledgeMCPClient | None = None

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SERVER_NAME = "enterprise_knowledge_mcp"
TOOL_NAME = f"mcp__{SERVER_NAME}__knowledge_search"
REQUISITION_SERVER_NAME = "oa_material_requisition_mcp"
REQUISITION_TOOL_NAMES = (
    "submit_requisition",
    "query_requisition",
)
AGENT_MODEL = "qwen3.8-2.4t-a95b"
AGENT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

def _create_agent(settings: Settings) -> AIAgent:
    """创建请求级 Agent，避免并发请求共享内部会话和流状态。"""
    return AIAgent(
        provider="alibaba",
        model=AGENT_MODEL,
        api_key=settings.dashscope_api_key,
        base_url=AGENT_BASE_URL,
        quiet_mode=True,
        disabled_toolsets=[
            f"mcp-{SERVER_NAME}",
            ATTENDANCE_TOOLSET,
            f"mcp-{FINANCE_SERVER_NAME}",
            f"mcp-{CRM_SERVER_NAME}",
        ],  # 这里禁止分类阶段自由调用考勤 Tool，由现有业务执行器在认证身份明确后调用。Tool 仍然真实注册在 Hermes registry 中，不需要新增 MCP 服务或修改 config/hermes.yaml。
        # skills_dir="app/hermes/skills",
        # tools_dir="app/hermes/tools",
        # mcp_dir="app/hermes/mcp",
    )

async def init_hermes_agent(settings: Settings):
    """初始化hermes_agent"""

    global _agent_settings, _knowledge_client

    try:
        # 注册 attendance tool
        register_attendance_tool(asyncio.get_running_loop())

        servers = _mcp_config(settings)
        knowledge_server = servers.pop(SERVER_NAME)
        if servers:
            await asyncio.to_thread(register_mcp_servers, servers)
        for tool_name in REQUISITION_TOOL_NAMES:
            registry_name = (
                f"mcp__{REQUISITION_SERVER_NAME}__{tool_name}"
            )
            if registry.get_entry(registry_name) is None:
                raise RuntimeError(
                    f"物资申领 MCP 工具注册失败: {registry_name}"
                )

        for tool_name in FINANCE_TOOL_NAMES:
            registry_name = (
                f"mcp__{FINANCE_SERVER_NAME}__{tool_name}"
            )
            if registry.get_entry(registry_name) is None:
                raise RuntimeError(
                    f"财务 MCP 工具注册失败: {registry_name}"
                )
        for tool_name in CRM_TOOL_NAMES:
            registry_name = f"mcp__{CRM_SERVER_NAME}__{tool_name}"
            if registry.get_entry(registry_name) is None:
                raise RuntimeError(
                    f"CRM MCP 工具注册失败: {registry_name}"
                )

        _knowledge_client = KnowledgeMCPClient(
            knowledge_server, cwd=str(PROJECT_ROOT),
            concurrency=settings.rag_query_concurrency,
        )
        discovered = await _knowledge_client.start()
        tool = next((tool for tool in discovered if tool.name == "knowledge_search"), None)
        if tool is None:
            raise RuntimeError("知识 MCP 未发现 knowledge_search")
        registry.register(
            name=TOOL_NAME, toolset=f"mcp-{SERVER_NAME}",
            schema={"name": TOOL_NAME, "description": tool.description or "知识库检索",
                    "parameters": tool.inputSchema},
            handler=_knowledge_client.handler, override=True,
        )

        if registry.get_entry(TOOL_NAME) is None:
            raise RuntimeError("knowledge_search MCP工具注册失败")

        await asyncio.to_thread(_create_agent, settings)
        _agent_settings = settings

        print("Hermes Agent 初始化成功，已进入 READY 状态")
        print(f"当前工作目录: {os.getcwd()}")
    except Exception as e:
        print(f"Hermes Agent 初始化失败:{e}")
        _agent_settings = None
        if _knowledge_client is not None:
            await _knowledge_client.close()
            _knowledge_client = None
        raise
    return _agent_settings

def _mcp_config(settings: Settings) -> dict:
    path = PROJECT_ROOT / "config" / "hermes.yaml"
    with path.open(encoding="utf-8") as stream:
        config = yaml.safe_load(stream)

    server = dict(config["mcp_servers"][SERVER_NAME])
    server["command"] = sys.executable

    # 将父进程实际生效的配置传给子进程，避免配置不一致
    environment = {}

    for name, value in settings:
        if value is None:
            continue
        if isinstance(value, SecretStr):
            value = value.get_secret_value()
        elif isinstance(value, (list, dict, bool)):
            value = json.dumps(value, ensure_ascii=False)
        else:
            value = str(value)

        environment[name.upper()] = value

    environment["PYTHONPATH"] = str(PROJECT_ROOT)
    environment["PYTHONUNBUFFERED"] = "1"

    server["env"] = environment
    servers = dict(config["mcp_servers"])
    servers[SERVER_NAME] = server

    finance = dict(servers[FINANCE_SERVER_NAME])
    finance["command"] = sys.executable
    finance["env"] = dict(environment)
    servers[FINANCE_SERVER_NAME] = finance

    crm = dict(servers[CRM_SERVER_NAME])
    crm["command"] = sys.executable
    crm["env"] = dict(environment)
    servers[CRM_SERVER_NAME] = crm

    return servers


async def shutdown_hermes_agent():
    global _agent_settings, _knowledge_client
    _agent_settings = None
    client, _knowledge_client = _knowledge_client, None
    if client is not None:
        await client.close()
    await asyncio.to_thread(shutdown_mcp_servers)

def get_agent() -> AIAgent | None:
    """为当前请求创建独立 Agent 实例。"""
    if _agent_settings is None:
        return None
    return _create_agent(_agent_settings)

async def call_knowledge_search(
    request: KnowledgeSearchRequest,
    user: User
) -> KnowledgeSearchResponse:
    if _agent_settings is None:
        raise RAGError("Hermes尚未初始化")

    if _agent_settings.knowledge_maintenance:
        raise RAGError("知识库维护中")
    try:
        with query_scope(_agent_settings.rag_query_timeout_seconds) as budget, phase("caller_total"):
            async with asyncio.timeout(budget.remaining()):
                arguments = request.model_dump(mode="json")
                arguments["identity_token"] = issue_identity_token(user, _agent_settings)
                raw = await asyncio.to_thread(registry.dispatch, TOOL_NAME, arguments)
                return _decode_knowledge_result(raw)
    except TimeoutError:
        raise RAGError("知识检索超时") from None


def _decode_knowledge_result(raw) -> KnowledgeSearchResponse:

    try:
        envelope = json.loads(raw) if isinstance(raw, str) else raw

        if not isinstance(envelope,dict):
            raise ValueError("无效的MCP工具返回格式")

        if envelope.get("error"):
            raise RAGError("RAG工具执行失败，请检查服务日志")

        payload = envelope.get("structuredContent")
        if payload is None:
            payload = envelope.get("result")

        if isinstance(payload,str):
            payload = json.loads(payload)

        return KnowledgeSearchResponse.model_validate(payload)
    except RAGError:
        raise
    except (ValueError, TypeError):
        raise RAGError("知识工具返回格式错误") from None
