import os

from run_agent import AIAgent

from app.config.settings import Settings

_agent_settings: Settings | None = None


def _create_agent(settings: Settings) -> AIAgent:
    """创建请求级 Agent，避免并发请求共享内部会话和流状态。"""
    return AIAgent(
        provider="alibaba",
        model="qwen3.8-27b",
        api_key=settings.dashscope_api_key,
        base_url=(
            "https://dashscope.aliyuncs.com/"
            "compatible-mode/v1"
        ),
        quiet_mode=True,
        # skills_dir="app/hermes/skills",
        # tools_dir="app/hermes/tools",
        # mcp_dir="app/hermes/mcp",
    )

async def init_hermes_agent(settings: Settings):
    """初始化hermes_agent"""

    global _agent_settings

    try:
        _create_agent(settings)
        _agent_settings = settings

        print("Hermes Agent 初始化成功，已进入 READY 状态")
        print(f"当前工作目录: {os.getcwd()}")
    except Exception as e:
        print(f"Hermes Agent 初始化失败:{e}")
        _agent_settings = None
    return _agent_settings

async def shutdown_hermes_agent():
    global _agent_settings
    _agent_settings = None

def get_agent() -> AIAgent | None:
    """为当前请求创建独立 Agent 实例。"""
    if _agent_settings is None:
        return None
    return _create_agent(_agent_settings)
