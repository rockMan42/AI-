# 封装会话状态
import json
import logging
from datetime import date, datetime

from app.constant.intent_state import IntentState
from app.core.redis_client import get_cache, set_cache

SESSION_TTL = 60 * 30
MAX_SUSPENDED_CONTEXTS=5
TERMINAL_STATES = {
    IntentState.SKILL_COMPLETED.value,
    IntentState.SKILL_FAILED.value,
}

def build_session_key(session_id: str) -> str:
    """获取会话元数据的key"""
    return f"dep:sess:meta:{session_id}"

async def get_intent_session(session_id: str) -> dict:
    value = await get_cache(build_session_key(session_id))

    if not value:
        return {}

    if isinstance(value,bytes):
        value = value.decode("utf-8")

    # 把json字符串转换为字典
    return json.loads(value)

async def save_intent_session(session_id: str, state: IntentState, **fields) -> dict:
    """
    保存会话状态
    :param session_id:
    :param state:
    :param fields:
    :return:
    """
    # 先获取会话状态 拿出来就是可用字典，放进去需要转成字符串
    session = await get_intent_session(session_id)

    # 更新会话状态
    session.update({
        "state": state.value,
        "updated_at":datetime.now().isoformat(),
        **fields
    })

    # 保存会话状态（使用json_dump转成字符串）
    await set_cache(build_session_key(session_id), json.dumps(session,ensure_ascii=False), SESSION_TTL)

    return session


async def switch_intent_session(
        session_id: str,
        new_intent: str,
        skill_name: str,
        confidence: float,
        slots: dict
) -> dict:
    """"
    切换意图。

    1. 新意图与当前意图相同：合并槽位，继续原任务。
    2. 新意图与当前意图不同：挂起旧任务，切换到新任务。
    """

    session = await get_intent_session(session_id)
    active_context = session.get("active_context")
    suspended_contexts = session.get("suspended_contexts",[])

    if not isinstance(suspended_contexts, list):
        suspended_contexts = []

    slots = slots or {}
    switch = False

    if active_context:
        old_intent = active_context.get("intent")
        old_state = active_context.get("workflow_state")

        if old_intent == new_intent:
            # 同一个意图，合并槽位
            merged_slots = {**active_context.get("slots",{}), **slots}
            slots = merged_slots
        elif old_state not in TERMINAL_STATES:
            active_context["suspended_at"] = datetime.now().isoformat()
            suspended_contexts.append(active_context)

            # 限制挂起任务数量
            suspended_contexts = suspended_contexts[-MAX_SUSPENDED_CONTEXTS:]

            switch = True



    new_active_context = {
        "intent": new_intent,
        "skill_name": skill_name,
        "confidence": confidence,
        "slots": slots,
        "workflow_state": IntentState.INTENT_MATCHED.value,
        "started_at": datetime.now().isoformat(),
    }

    await save_intent_session(
        session_id,
        IntentState.INTENT_MATCHED,
        current_intent=new_intent,
        skill_name=skill_name,
        confidence=confidence,
        slots=slots,
        active_context=new_active_context,
        suspended_contexts=suspended_contexts,
        unknown_count=0
    )


    logging.info(f"Switching intent to {new_intent}\n,新的活跃的意图信息:{json.dumps(new_active_context, ensure_ascii=False)}\n,挂起的意图信息:{json.dumps(suspended_contexts, ensure_ascii=False)}")



    return {
        "switch": switch,
        "session":session
    }


async def update_active_skill_state(
        session_id: str,
        state: IntentState
) -> dict:
    session = await get_intent_session(session_id)
    active_context = session.get("active_context")

    active_context["workflow_state"] = state.value
    active_context["updated_at"] = datetime.now().isoformat()

    return await save_intent_session(session_id,state,active_context=active_context)


async def resume_suspended_intent(
        session_id: str,
        intent: str
) -> dict | None:
    """
    恢复被挂起的意图
    如果没有传意图，则恢复最近一条被挂起的意图
    :param session_id: 
    :param intent: 
    :return: 
    """

    session = await get_intent_session(session_id)
    suspended_contexts = session.get("suspended_contexts")

    if not suspended_contexts:
        return None

    resume_index = None

    if intent is None:
        resume_index = len(suspended_contexts) - 1
    else:
        for index in range(len(suspended_contexts)-1,-1,-1):
            if suspended_contexts[index]["intent"] == intent:
                resume_index = index
                break

    if resume_index is None:
        return None

    resume_context = suspended_contexts.pop(resume_index)
    resume_context["workflow_state"] = IntentState.INTENT_MATCHED.value
    resume_context["resume_at"] = datetime.now().isoformat()

    await save_intent_session(
        session_id,
        IntentState.INTENT_MATCHED,
        current_intent=resume_context.get("intent"),
        skill_name=resume_context.get("skill_name"),
        confidence=resume_context.get("confidence",1.0),
        slots=resume_context.get("slots",{}),
        active_context=resume_context,
        suspended_contexts=suspended_contexts,
    )

    return resume_context




