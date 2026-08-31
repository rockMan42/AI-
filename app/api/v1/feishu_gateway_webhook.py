import asyncio
import logging
import json
from fastapi import Request, APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.config.settings import get_settings
from app.core.database import get_session
from app.core.redis_client import set_cache, get_cache
from app.hermes.agent import get_agent
from app.services.register_skill import SkillRegistry, get_skill_register
from app.services.user import get_or_create_user
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
                            
                            请你严格以以下 JSON 格式输出，不要有其他内容！！！：
                            {"intent": "意图编码", "confidence": 0.0~1.0, "extracted_slots": {已提取的槽位}}
                            
                            如果无法确定意图，confidence 设为 0。"""


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
        result = await asyncio.wait_for(
            asyncio.to_thread(
                agent.run_conversation,
                user_message=text,
                system_message=SYSTEM_MESSAGE,
                task_id=message_id,
            ),
            timeout=300,  # 故意设置极短，稳定触发超时
        )
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

    # 具体逻辑
    intent_router = IntentRouter(register)
    router_result = intent_router.router(json.loads(reply),user.user_id)
    action_ = router_result["action"]

    # 根据 action 的值进行不同的处理 如果是 execute_skill 则执行 skill 否则返回消息（针对中等和低等置信度）
    if action_ == "execute_skill":
        skill_name = router_result["skill"].name
        if skill_name == "performance_query" and user.role not in ["admin", "manager"]:
            reply_text = "该功能仅限主管及以上角色使用"
        else:
            reply_text = "执行skill" + "路由到:" + skill_name
    else:
        reply_text = router_result["message"]

    # 通过飞书 API 发送回复
    await _send_feishu_reply(message_id,reply_text)

    return response.success_response("飞书回复成功")