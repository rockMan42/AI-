import time

from app.constant.intent_state import IntentState
from app.services.conversation_engine.register_skill import SkillSchema
from app.services.conversation_engine.session_store import SessionStore
from app.services.conversation_engine.slot_manage import (
    MAX_SUSPENDED_CONTEXTS,
    TERMINAL_WORKFLOW_STATES,
    SessionSlots,
    SlotState,
)


class ConversationManager:
    """管理活动意图、挂起事务、恢复和意图状态。"""

    def __init__(self, store: SessionStore):
        self.store = store

    async def get_session(
        self,
        session_id: str,
    ) -> SessionSlots | None:
        return await self.store.load(session_id)

    async def update_intent_state(
        self,
        session_id: str,
        user_id: str | int,
        state: IntentState,
    ) -> SessionSlots:
        session = await self.store.load(session_id)
        if session is None:
            session = SessionSlots(
                session_id=session_id,
                user_id=user_id,
            )

        session.state = state.value
        await self.store.save(session)
        return session

    async def get_or_create_session(
        self,
        user_id: str | int,
        intent_code: str,
        skill: SkillSchema,
        confidence: float = 1.0,
    ) -> SessionSlots:
        """创建、延续或恢复指定意图的事务。"""
        session_id = str(user_id)
        session = await self.store.load(session_id)

        if session is None:
            session = SessionSlots(
                session_id=session_id,
                user_id=user_id,
            )
            self._start_intent(
                session,
                intent_code,
                skill,
                confidence,
            )
        elif self._can_continue(session, intent_code):
            session.confidence = confidence
            self._sync_skill_slots(session, skill)
        else:
            self._suspend_active_context(session)
            restored = self._restore_suspended_context(
                session,
                intent_code,
            )
            if restored:
                session.confidence = confidence
                self._sync_skill_slots(session, skill)
            else:
                self._start_intent(
                    session,
                    intent_code,
                    skill,
                    confidence,
                )

        session.state = IntentState.INTENT_MATCHED.value
        session.workflow_state = IntentState.INTENT_MATCHED.value
        session.status = "collecting"
        session.pending_intent = None
        session.unknown_count = 0
        await self.store.save(session)
        return session

    async def switch_intent(
        self,
        current_session: SessionSlots | None,
        new_user_id: str | int,
        new_intent: str,
        new_skill: SkillSchema,
        confidence: float = 1.0,
    ) -> SessionSlots:
        """兼容附件接口；挂起和恢复逻辑统一由 get_or_create_session 完成。"""
        return await self.get_or_create_session(
            user_id=new_user_id,
            intent_code=new_intent,
            skill=new_skill,
            confidence=confidence,
        )

    async def mark_ambiguous(
        self,
        session_id: str,
        user_id: str | int,
        suggested_intent: str,
    ) -> SessionSlots:
        session = await self.update_intent_state(
            session_id,
            user_id,
            IntentState.INTENT_AMBIGUOUS,
        )
        session.pending_intent = suggested_intent
        await self.store.save(session)
        return session

    async def record_unknown(
        self,
        session_id: str,
        user_id: str | int,
    ) -> int:
        session = await self.update_intent_state(
            session_id,
            user_id,
            IntentState.INTENT_UNKNOWN,
        )
        session.unknown_count += 1
        await self.store.save(session)
        return session.unknown_count

    async def update_workflow_state(
        self,
        session_id: str,
        state: IntentState,
    ) -> SessionSlots | None:
        session = await self.store.load(session_id)
        if session is None:
            return None

        session.state = state.value
        session.workflow_state = state.value
        if state == IntentState.SKILL_FAILED:
            session.status = "execution_failed"
        elif state == IntentState.SKILL_COMPLETED:
            session.status = "completed"
        await self.store.save(session)
        return session

    async def prepare_retry(
        self,
        session_id: str,
    ) -> SessionSlots | None:
        """恢复执行失败但槽位完整的事务，供用户显式重试。"""
        session = await self.store.load(session_id)
        if session is None:
            return None
        if session.workflow_state != IntentState.SKILL_FAILED.value:
            return None
        if session.status not in {"completed", "execution_failed"}:
            return None
        if not session.all_required_filled():
            return None

        session.state = IntentState.INTENT_MATCHED.value
        session.workflow_state = IntentState.INTENT_MATCHED.value
        session.status = "collecting"
        session.pending_slot = None
        session.pending_intent = None
        await self.store.save(session)
        return session

    def _can_continue(
        self,
        session: SessionSlots,
        intent_code: str,
    ) -> bool:
        return (
            session.intent_code == intent_code
            and session.workflow_state
            not in TERMINAL_WORKFLOW_STATES
            and session.status not in {"completed", "expired"}
        )

    def _suspend_active_context(
        self,
        session: SessionSlots,
    ) -> None:
        if not session.intent_code:
            return
        if session.workflow_state in TERMINAL_WORKFLOW_STATES:
            return
        if session.status in {"completed", "expired"}:
            return

        session.suspended_contexts.append(
            session.active_context_to_dict()
        )
        session.suspended_contexts = session.suspended_contexts[
            -MAX_SUSPENDED_CONTEXTS:
        ]

    def _restore_suspended_context(
        self,
        session: SessionSlots,
        intent_code: str,
    ) -> bool:
        for index in range(
            len(session.suspended_contexts) - 1,
            -1,
            -1,
        ):
            context = session.suspended_contexts[index]
            context_intent = context.get(
                "intent_code",
                context.get("intent"),
            )
            if context_intent != intent_code:
                continue

            session.suspended_contexts.pop(index)
            if "intent_code" not in context:
                context = self._convert_legacy_context(context)
            session.restore_active_context(context)
            return True

        return False

    def _start_intent(
        self,
        session: SessionSlots,
        intent_code: str,
        skill: SkillSchema,
        confidence: float,
    ) -> None:
        session.intent_code = intent_code
        session.confidence = confidence
        session.slots = {}
        self._sync_skill_slots(session, skill)
        session.workflow_state = IntentState.INTENT_MATCHED.value
        session.status = "collecting"
        session.pending_slot = None
        session.pending_intent = None
        session.turn_count = 0
        session.dynamic_category = None
        session.dynamic_slot_names = []
        session.intent_started_at = time.time()

    def _sync_skill_slots(
        self,
        session: SessionSlots,
        skill: SkillSchema,
    ) -> None:
        for definition in skill.slots:
            if definition.name in session.slots:
                continue
            session.slots[definition.name] = SlotState(
                name=definition.name,
                type=definition.type,
                required=definition.required,
                description=definition.description,
                enum=list(definition.enum),
                default=definition.default,
                follow_up_prompt=definition.follow_up_prompt,
            )

    def _convert_legacy_context(self, context: dict) -> dict:
        slots = {
            name: {
                "name": name,
                "value": value,
                "filled": value is not None,
            }
            for name, value in (context.get("slots") or {}).items()
        }
        return {
            "intent_code": context.get("intent", ""),
            "confidence": context.get("confidence", 0.0),
            "slots": slots,
            "workflow_state": context.get(
                "workflow_state",
                "matched",
            ),
            "status": "suspended",
            "pending_slot": None,
            "turn_count": 0,
            "dynamic_category": None,
            "dynamic_slot_names": [],
            "intent_started_at": time.time(),
        }
