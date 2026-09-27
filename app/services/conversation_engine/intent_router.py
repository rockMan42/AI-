import logging

from app.services.conversation_engine.register_skill import SkillRegistry



log = logging.getLogger(__name__)

CONFIRM_THRESHOLD = 0.7
FALLBACK_THRESHOLD = 0.4

class IntentRouter:
    def __init__(self, register: SkillRegistry):
        self.register = register


    def router(self, llm_result, user_id: str) -> dict:
        """
                根据 LLM 意图分类结果进行路由

                Args:
                    llm_result: LLM 输出的 JSON，包含 intent、confidence、extracted_slots
                    user_id: 当前用户 ID

                Returns:
                    路由结果字典，包含 action 和相关数据
                """

        intent = llm_result.get("intent")
        confidence = float(llm_result.get("confidence") or 0.0)
        extracted_slots = llm_result.get("extracted_slots") or {}

        # 高置信度
        if confidence >= CONFIRM_THRESHOLD:
            skill = self.register.get_skill(intent)

            if skill is not None:
                return {
                    "action":"execute_skill",
                    "skill":skill,
                    "slots":extracted_slots,
                    "confidence":confidence
                }

            log.error(f"Skill {intent} not found", exc_info=True)

            # 若是技能不存在，则 fallback 降级处理
            return {
                "action":"fallback",
                "message":self._build_fallback_messages()
            }

        # 中置信度 向用户确认
        if FALLBACK_THRESHOLD <= confidence < CONFIRM_THRESHOLD:
            skill = self.register.get_skill(intent)
            if skill is None:
                return {
                    "action":"fallback",
                    "message":self._build_fallback_messages()
                }

            display_name = skill.display_name or "办理这项业务"
            return {
                "action":"confirm_intent",
                "suggested_intent":intent,
                "confidence":confidence,
                "message":f"我理解您是想{display_name}，对吗？请回复“确认”继续，或重新描述。"
            }

        # 低置信度
        return {
            "action":"fallback",
            "message":self._build_fallback_messages()
        }


    def _build_fallback_messages(self) -> str:
        return (
            "我还没确定您要办理什么。可以试试：\n"
            "查本月考勤\n"
            "我要请假\n"
            "查询报销进度\n"
            "也可以直接描述您要办理的事情。"
        )
