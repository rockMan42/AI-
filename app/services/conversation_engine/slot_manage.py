import logging
import time
from dataclasses import dataclass, field, asdict
from typing import Any

logger = logging.getLogger(__name__)

# 会话超时时间（30分钟）
SESSION_TTL = 60 * 30
# 最大轮数
MAX_TURNS = 10
# 最大挂起会话数
MAX_SUSPENDED_CONTEXTS = 5
# 终止工作流状态
TERMINAL_WORKFLOW_STATES = {"completed", "failed"}

@dataclass
class SlotState:
    """单个槽位的定义，当前值和追问状态"""
    name: str
    type: str = "string"
    required: bool = False
    description: str = ""
    enum: list[str] = field(default_factory=list)
    default: Any = None
    follow_up_prompt: str | None = None
    value: Any = None
    filled: bool = False
    asked: bool = False
    asked_count: int = 0
    error: str | None = None

    @classmethod
    def from_dict(cls, data: dict) -> "SlotState":
        """兼容当前版本及旧版 Redis 中的槽位结构。"""
        return cls(
            name=data["name"],
            type=data.get("type", "string"),
            required=bool(data.get("required", False)),
            description=data.get("description", ""),
            enum=list(data.get("enum") or []),
            default=data.get("default"),
            follow_up_prompt=data.get("follow_up_prompt"),
            value=data.get("value"),
            filled=bool(data.get("filled", False)),
            asked=bool(data.get("asked", False)),
            asked_count=int(data.get("asked_count", 0)),
            error=data.get("error"),
        )




@dataclass
class SessionSlots:
    """一个用户的活动事物，槽位状态以及挂起事物"""
    session_id: str
    user_id: str | int
    intent_code: str = ""
    slots: dict[str, SlotState] = field(default_factory=dict)
    confidence: float = 0.0
    state: str = "pending"
    workflow_state: str = "pending"
    status: str = "idle"
    pending_slot: str | None = None
    pending_intent: str | None = None
    turn_count: int = 0
    unknown_count: int = 0
    dynamic_category: str | None = None
    dynamic_slot_names: list[str] = field(default_factory=list)
    suspended_contexts: list[dict] = field(default_factory=list)
    created_at: float = 0.0
    last_active_at: float = 0.0
    intent_started_at: float = 0.0


    def __post_init__(self):
        now = time.time()
        if self.created_at == 0.0:
            self.created_at = now
        if self.last_active_at == 0.0:
            self.last_active_at = now
        if self.intent_code and self.intent_started_at == 0.0:
            self.intent_started_at = now

    def get_next_unfilled_required(self):
        """按 Skill YAML 中的声明顺序返回下一个缺失必填槽位。"""
        for slot in self.slots.values():
            if slot.required and not slot.filled:
                return slot
        return None

    def get_next_required_filled(self) -> SlotState | None:
        """也就是说，返回下一个必须且缺失的槽位。"""
        return self.get_next_unfilled_required()

    def all_required_filled(self) -> bool:
        """获取所有必填槽位是否已填满"""
        return self.get_next_unfilled_required() is None

    def filled_values(self) -> dict[str, Any]:
        """获取所有已填槽位的值"""
        return {
            name: slot.value
            for name, slot in self.slots.items()
            if slot.filled
        }

    def active_context_to_dict(self) -> dict:
        """生成可挂起、可恢复的活动事务快照。"""
        return {
            "intent_code": self.intent_code,
            "confidence": self.confidence,
            "slots": {
                name: asdict(slot)
                for name, slot in self.slots.items()
            },
            "workflow_state": self.workflow_state,
            "status": "suspended",
            "pending_slot": self.pending_slot,
            "turn_count": self.turn_count,
            "dynamic_category": self.dynamic_category,
            "dynamic_slot_names": list(self.dynamic_slot_names),
            "intent_started_at": self.intent_started_at,
            "suspended_at": time.time(),
        }

    def restore_active_context(self, context: dict) -> None:
        """恢复之前挂起的事务。"""
        self.intent_code = context.get("intent_code", "")
        self.confidence = float(context.get("confidence", 0.0))
        self.slots = {
            name: SlotState.from_dict({"name": name, **slot_data})
            for name, slot_data in (context.get("slots") or {}).items()
        }
        self.workflow_state = context.get("workflow_state", "matched")
        self.status = "collecting"
        self.pending_slot = context.get("pending_slot")
        self.pending_intent = None
        self.turn_count = int(context.get("turn_count", 0))
        self.dynamic_category = context.get("dynamic_category")
        self.dynamic_slot_names = list(
            context.get("dynamic_slot_names") or []
        )
        self.intent_started_at = float(
            context.get("intent_started_at", time.time())
        )

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "intent_code": self.intent_code,
            "slots": {
                name: asdict(slot)
                for name, slot in self.slots.items()
            },
            "confidence": self.confidence,
            "state": self.state,
            "workflow_state": self.workflow_state,
            "status": self.status,
            "pending_slot": self.pending_slot,
            "pending_intent": self.pending_intent,
            "turn_count": self.turn_count,
            "unknown_count": self.unknown_count,
            "dynamic_category": self.dynamic_category,
            "dynamic_slot_names": list(self.dynamic_slot_names),
            "suspended_contexts": self.suspended_contexts,
            "created_at": self.created_at,
            "last_active_at": self.last_active_at,
            "intent_started_at": self.intent_started_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SessionSlots":
        slots = {
            name: SlotState.from_dict({"name": name, **slot_data})
            for name, slot_data in (data.get("slots") or {}).items()
        }

        return cls(
            session_id=str(data["session_id"]),
            user_id=data.get("user_id", data["session_id"]),
            intent_code=data.get("intent_code", ""),
            slots=slots,
            confidence=float(data.get("confidence", 0.0)),
            state=data.get("state", "pending"),
            workflow_state=data.get(
                "workflow_state",
                data.get("state", "pending"),
            ),
            status=data.get("status", "idle"),
            pending_slot=data.get("pending_slot"),
            pending_intent=data.get("pending_intent"),
            turn_count=int(data.get("turn_count", 0)),
            unknown_count=int(data.get("unknown_count", 0)),
            dynamic_category=data.get("dynamic_category"),
            dynamic_slot_names=list(
                data.get("dynamic_slot_names") or []
            ),
            suspended_contexts=list(
                data.get("suspended_contexts") or []
            ),
            created_at=float(data.get("created_at", 0.0)),
            last_active_at=float(data.get("last_active_at", 0.0)),
            intent_started_at=float(
                data.get("intent_started_at", 0.0)
            ),
        )