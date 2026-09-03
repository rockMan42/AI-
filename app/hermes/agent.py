import os

from run_agent import AIAgent

from app.config.settings import Settings

_agent:AIAgent | None = None

async def init_hermes_agent(settings: Settings):
    """初始化hermes_agent"""

    global _agent

    try:
        _agent = AIAgent(
            provider="alibaba",
            model="qwen3.7-flash-2026-07-15",
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

        print("Hermes Agent 初始化成功，已进入 READY 状态")
        print(f"当前工作目录: {os.getcwd()}")
    except Exception as e:
        print(f"Hermes Agent 初始化失败:{e}")
    return _agent

async def shutdown_hermes_agent():
    global _agent
    if _agent:
        _agent = None

def get_agent() -> AIAgent:
    """获取Agent实例（供其他模块调用）"""
    return _agent
