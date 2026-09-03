import asyncio
import logging
import json
import re
from dataclasses import asdict
from fastapi import Request, APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.config.settings import get_settings
from app.core.database import get_session
from app.core.redis_client import set_cache, get_cache
from app.hermes.agent import get_agent
from app.models.scheme.skill_context import SkillContext
from app.services.register_skill import SkillRegistry, get_skill_register
from app.services.user import get_or_create_user
from app.skill_executor.registry import ExecutorRegistry
from app.utils.aes_cipher import AESCipher
from app.services.feishu import _verify_signature
from app.services.feishu import _send_feishu_reply
from app.utils import response
from app.services.intent_router import IntentRouter
"""
飞书网关webhook
"""
SYSTEM_MESSAGE: str = """你是 AI 数字员工平台的意图分类器。根据用户消息，从以下意图中选择最匹配的一个：
                            1. requisition_apply - 物资申领（领电脑、办公用品、设备配件等）
                            2. expense_reimburse - 差旅报销（报销出差费用、提交发票等）
                            3. attendance_query - 出勤查询（查考勤、打卡记录、工时等）
                            4. leave_apply - 请假申请（请假、休假、年假、事假等）
                            5. policy_query - 制度查询（公司制度、报销标准、规章制度等）
                            6. lead_query - 线索查询（客户线索、商机、销售数据等）
                            7. performance_query - 绩效查询（考核结果、绩效评分等）
                            
                            tips:extracted_slots的内容严格遵守skill的定义，请勿自行添加其他内容，尤其注意格式，校验等等！！！：
                            请你严格以以下 JSON 格式输出，不要有其他内容！！！：
                            {"intent": "意图编码", "confidence": 0.0~1.0, "extracted_slots": {已提取的槽位}}
                            
                            如果无法确定意图，confidence 设为 0。"""

REPLY_SYSTEM_MESSAGE = """
  1. 只输出纯文本，不要输出 JSON。
            2. 禁止使用 Markdown，包括 **、*、#、-、>、``` 等符号。
            3. 不要使用表格。
            4. 使用换行和中文标签组织内容。
            5. 不得修改、补充或猜测技能执行结果中的事实。
            6. 直接输出给用户看的内容，不要解释生成过程。
            7. 不要使用 -** ** 包裹文字
"""
log = logging.getLogger(__name__)
settings = get_settings()
router = APIRouter()

@router.post("/webhook/feishu")
async def feishu_webhook(request: Request,db: AsyncSession = Depends(get_session),register: SkillRegistry = Depends(get_skill_register)):
    """接受飞书事件，处理消息并调用Agent回复"""

    # 1. 获取飞书发送的原始 JSON 数据
    global token
    data = await request.json()


    # 2. 处理 URL 验证 (challenge)
    # 当飞书配置回调地址时，会发送一个 type 为 "url_verification" 的请求
    # 另外一个情况：当飞书发送“你好”产生的是 im.message.receive_v1，同样带有 encrypt，但解密后只有 schema/header/event，没有 challenge。
    if data.get("encrypt"):
        encrypt_key = settings.encrypt_key
        aes_cipher =  AESCipher(encrypt_key)
        decrypt_string = aes_cipher.decrypt_string(data["encrypt"])
        data = json.loads(decrypt_string)

    # 处理 URL 验证 (challenge) 区分普通消息和 URL 验证
    if data.get("type") == "url_verification":
        return {"challenge": data["challenge"]}


    # 3. 处理正常的事件消息 (例如 im.message.receive_v1)
    # 签名校验（防伪造请求）
    token = data.get("header").get("token")
    if not await _verify_signature(token, settings.feishu_verification_token):
        return {"code": 403, "msg": "Signature verification failed"}

    # 提取消息
    event = data.get("event", {})
    message = event.get("message", {})
    sender = event.get("sender", {})
    message_id = message.get("message_id", "")
    open_id = sender.get("sender_id", {}).get("open_id", "")
    msg_type = message.get("message_type", "")
    content = json.loads(message.get("content", "{}"))
    log.info(f"open_id:{open_id},text:{content.get('text', '')}")
    print(f"open_id:{open_id},text:{content.get('text', '')}")

    # 消息去重（飞书可能重复推送同一条消息）
    dedup_key=f"dep:sess:dedup:{message_id}"
    get_dedup = await get_cache(dedup_key)
    if get_dedup:
        return response.success_response("Message already processed")
    await set_cache(dedup_key, "1", 60 * 60 * 24)

    # 白名单校验
    allowed_users = settings.feishu_allowed_users.split(",")
    if open_id not in allowed_users:
        log.info(f"用户{open_id}不在白名单中,忽略消息")
        return response.success_response("User not allowed")

    # 仅处理文本消息
    if msg_type != "text":
        log.info(f"非文本消息,忽略消息")
        return response.success_response("Only text messages are allowed")

    text = content.get("text", "")

    # 获取或创建系统用户
    user = await get_or_create_user(open_id,db)

    # 调用 Hermes Agent 处理消息
    # task_id的主要目的是为每次对话或任务提供一个独立的、隔离的运行环境，确保任务之间的数据不相互影响
    # user_id 用于标识发送消息的用户,用户身份标识,会话管理,数据隔离（记忆）,权限控制
    agent = get_agent()
    try:
        result = await conversation(agent, text, SYSTEM_MESSAGE, message_id)
    except asyncio.TimeoutError:
        log.exception("LLM 请求超时")
        await _send_feishu_reply(message_id, "系统繁忙，请稍后重试")
        return response.success_response("LLM timeout")
    except Exception:
        log.exception("LLM 调用失败")
        await _send_feishu_reply(message_id, "系统繁忙，请稍后重试")
        return response.success_response("LLM failed")

    reply = result.get("final_response", "")
    print(f"reply:{reply}")

    # 路由skill
    intent_router = IntentRouter(register)
    llm_result = parse_llm_json(reply)

    router_result = intent_router.router(llm_result,user.user_id)
    action_ = router_result["action"]

    # 根据 action 的值进行不同的处理 如果是 execute_skill 则执行 skill 否则返回消息（针对中等和低等置信度）
    if action_ == "execute_skill":
        # 校验用户权限 TODO
        # if skill_name == "performance_query" and user.role not in ["admin", "manager"]:
        #     reply_text = "该功能仅限主管及以上角色使用"
        # else:
        skill_name = router_result["skill"].name
        executor_registry = ExecutorRegistry()
        executor = executor_registry.get_executor(skill_name)
        if executor:
            context = SkillContext(
                user_id=user.user_id,
                open_id=user.feishu_open_id,
                role=user.role,
                department_id=user.department_id,
                session_id=str(user.user_id),
                message_id=message_id
            )
            extracted_slots = router_result["slots"]
            skill_result = await executor.executor(context, extracted_slots, db)

            # SkillResult 对象，不能直接与字符串拼接，需要转换为字符串 使用 json.dumps
            prompt = (
                f"用户问题：{text}\n"
                f"技能执行结果：{json.dumps(asdict(skill_result), ensure_ascii=False)}\n"
                "请根据技能执行结果生成简洁、自然的用户回复。"
            )

            result = await conversation(agent, prompt, REPLY_SYSTEM_MESSAGE, message_id)
            reply_text = result.get("final_response", skill_result.message)
        else:
            reply_text = "技能执行器未找到"
    else:
        reply_text = router_result["message"]

    # 通过飞书 API 发送回复
    await _send_feishu_reply(message_id, clean_markdown(reply_text))

    return response.success_response("飞书回复成功")

# 封装对话逻辑
async def conversation(agent, text, system_message, message_id) -> dict:
    return await asyncio.wait_for(
        asyncio.to_thread(
            agent.run_conversation,
            user_message=text,
            system_message=system_message,
            task_id=message_id,
        ),
        timeout=3000,  # 可以故意设置极短0.1，稳定触发超时
    )


# 清理 Markdown 格式
def clean_markdown(text: str) -> str:
    text = re.sub(r"[*_`#>]", "", text)
    text = re.sub(r"(?m)^\s*-\s+", "", text)
    return text.strip()

# 解析模型返回内容
def parse_llm_json(reply: str) -> dict:
    start = reply.find("{")
    if start == -1:
        raise ValueError(f"模型未返回 JSON：{reply}")

    decoder = json.JSONDecoder()
    data, _ = decoder.raw_decode(reply[start:])
    return data