import logging

from app.services.register_skill import SkillRegistry



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
        confidence = float(llm_result.get("confidence"))
        extracted_slots = llm_result.get("extracted_slots")

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
            return {
                "action":"confirm_intent",
                "suggested_intent":intent,
                "confidence":confidence,
                "message":f"请确认您的意图是否为{intent}？请确认或者重新描述"
            }

        # 低置信度
        return {
            "action":"fallback",
            "message":self._build_fallback_messages()
        }


    def _build_fallback_messages(self) -> str:
        skills = self.register.get_all_skills()
        skill_list= "\n".join(f"- {s.description}" for s in skills.values())
        return ("抱歉，我没能理解您的意思，你可以试试以下操作:\n" +
                f"{skill_list}\n" +
                "请你用简单的话描述你想做的事情")

